import os
import uuid
import time
import json
import threading
import tempfile
from datetime import date
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai
import stripe

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max upload

# In-memory job store
jobs = {}

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_SECRET_KEY = os.environ.get('SUPABASE_SECRET_KEY', '')
DAILY_FREE_LIMIT = 1  # anonymous uploads per IP per day

STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

# Supabase client (optional — app works without it)
db = None
if SUPABASE_URL and SUPABASE_SECRET_KEY:
    try:
        from supabase import create_client
        db = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
        print("Supabase connected.")
    except Exception as e:
        print(f"Supabase init failed: {e}")

# Stripe
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
    print("Stripe configured.")

MUAY_THAI_PROMPT = """You are an experienced Muay Thai coach with 20+ years training fighters at all levels — from beginners to competitive fighters. Analyze this training video and give specific, technical coaching feedback.

Structure your response exactly like this:

## Overall Assessment
2-3 sentences on what you observe — skill level, energy, what immediately stands out.

## What's Working
Specific positives — reference actual things you see happening in the video. Be concrete, not generic.

## Technique Breakdown
Go through what you observe and give technical notes on each:
- Guard and stance
- Footwork and movement
- Strikes (jab, cross, hook, elbow, teep, roundhouse, knee — whatever appears)
- Defense and head movement
- Combinations and rhythm

## Top 3 Corrections
The three most impactful technical fixes, each explained clearly:

**1. [Issue]**
What you see → What it should look like → Why it matters for effectiveness or safety

**2. [Issue]**
What you see → What it should look like → Why it matters

**3. [Issue]**
What you see → What it should look like → Why it matters

## Drills to Focus On
1-2 specific drills or exercises that directly address the main corrections above.

Be direct and technical. Talk like a real coach — not a chatbot. Reference specific things you actually see. If you cannot clearly see the technique due to video quality or angle, say so."""


# ---------- Supabase helpers ----------

def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr or 'unknown'


def check_daily_limit(ip_address):
    if not db:
        return True
    try:
        today = date.today().isoformat()
        result = db.table('daily_usage').select('upload_count').eq('ip_address', ip_address).eq('date', today).execute()
        if result.data and result.data[0]['upload_count'] >= DAILY_FREE_LIMIT:
            return False
        return True
    except Exception as e:
        print(f"Rate limit check error: {e}")
        return True


def increment_daily_usage(ip_address):
    if not db:
        return
    try:
        today = date.today().isoformat()
        existing = db.table('daily_usage').select('id, upload_count').eq('ip_address', ip_address).eq('date', today).execute()
        if existing.data:
            new_count = existing.data[0]['upload_count'] + 1
            db.table('daily_usage').update({'upload_count': new_count}).eq('id', existing.data[0]['id']).execute()
        else:
            db.table('daily_usage').insert({'ip_address': ip_address, 'date': today, 'upload_count': 1}).execute()
    except Exception as e:
        print(f"Increment usage error: {e}")


def log_analysis_start(job_id, ip_address, video_filename, video_size_mb):
    if not db:
        return
    try:
        db.table('analyses').insert({
            'id': job_id,
            'ip_address': ip_address,
            'status': 'processing',
            'video_filename': video_filename,
            'video_size_mb': round(video_size_mb, 2),
            'gemini_model': 'gemini-2.5-flash',
            'discipline': 'muay_thai',
        }).execute()
    except Exception as e:
        print(f"Log start error: {e}")


def log_analysis_complete(job_id, result_markdown, processing_seconds):
    if not db:
        return
    try:
        db.table('analyses').update({
            'status': 'complete',
            'result_markdown': result_markdown,
            'processing_seconds': round(processing_seconds, 1),
        }).eq('id', job_id).execute()
    except Exception as e:
        print(f"Log complete error: {e}")


def log_analysis_error(job_id, error_message):
    if not db:
        return
    try:
        db.table('analyses').update({
            'status': 'error',
            'error_message': error_message,
        }).eq('id', job_id).execute()
    except Exception as e:
        print(f"Log error: {e}")


# ---------- Video analysis ----------

def analyze_video(job_id, video_path, mime_type, ip_address, video_filename, video_size_mb):
    start_time = time.time()
    try:
        if not GEMINI_API_KEY:
            raise ValueError('GEMINI_API_KEY not set.')

        genai.configure(api_key=GEMINI_API_KEY)

        jobs[job_id]['status'] = 'uploading'
        jobs[job_id]['step'] = 'Uploading video to Gemini...'

        video_file = genai.upload_file(video_path, mime_type=mime_type)

        jobs[job_id]['status'] = 'processing'
        jobs[job_id]['step'] = 'Gemini is processing your video...'

        poll_count = 0
        while video_file.state.name == "PROCESSING":
            time.sleep(3)
            poll_count += 1
            video_file = genai.get_file(video_file.name)
            if poll_count > 60:
                raise TimeoutError("Video processing timed out")

        if video_file.state.name == "FAILED":
            raise ValueError("Gemini could not process this video file. Try a different format (MP4 works best).")

        jobs[job_id]['step'] = 'Analyzing your technique...'

        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(
            [video_file, MUAY_THAI_PROMPT],
            generation_config=genai.GenerationConfig(
                temperature=0.4,
                max_output_tokens=8192,
            )
        )

        try:
            genai.delete_file(video_file.name)
        except Exception:
            pass

        processing_seconds = time.time() - start_time
        jobs[job_id]['status'] = 'complete'
        jobs[job_id]['result'] = response.text
        jobs[job_id]['step'] = 'Done'

        log_analysis_complete(job_id, response.text, processing_seconds)

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = str(e)
        log_analysis_error(job_id, str(e))
    finally:
        try:
            os.unlink(video_path)
        except Exception:
            pass


@app.route('/')
def index():
    return render_template('index.html', api_configured=bool(GEMINI_API_KEY))


@app.route('/upload', methods=['POST'])
def upload():
    if not GEMINI_API_KEY:
        return jsonify({'error': 'GEMINI_API_KEY not configured on the server.'}), 500

    if 'video' not in request.files:
        return jsonify({'error': 'No video file in request.'}), 400

    video = request.files['video']
    if not video.filename:
        return jsonify({'error': 'No file selected.'}), 400

    ext = video.filename.rsplit('.', 1)[-1].lower() if '.' in video.filename else 'mp4'
    mime_map = {
        'mp4': 'video/mp4',
        'mov': 'video/quicktime',
        'avi': 'video/avi',
        'webm': 'video/webm',
        'mkv': 'video/x-matroska',
        'm4v': 'video/mp4',
    }
    mime_type = mime_map.get(ext, 'video/mp4')

    # Rate limit anonymous users
    ip = get_client_ip()
    if not check_daily_limit(ip):
        return jsonify({'error': 'daily_limit', 'message': "You've used your free upload for today. Upgrade to Waikru Starter for more."}), 429

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}')
    video.save(tmp.name)
    tmp.close()

    video_size_mb = os.path.getsize(tmp.name) / (1024 * 1024)
    job_id = str(uuid.uuid4())
    jobs[job_id] = {'status': 'queued', 'step': 'Starting...'}

    log_analysis_start(job_id, ip, video.filename, video_size_mb)
    increment_daily_usage(ip)

    t = threading.Thread(
        target=analyze_video,
        args=(job_id, tmp.name, mime_type, ip, video.filename, video_size_mb),
        daemon=True
    )
    t.start()

    return jsonify({'job_id': job_id})


@app.route('/api/status/<job_id>')
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


@app.route('/results/<job_id>')
def results(job_id):
    job = jobs.get(job_id)
    if not job:
        return "Job not found", 404
    return render_template('results.html', job_id=job_id)


@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        return jsonify({'error': 'Stripe not configured.'}), 500
    try:
        host = request.host_url.rstrip('/')
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{'price': STRIPE_PRICE_ID, 'quantity': 1}],
            mode='subscription',
            success_url=f"{host}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{host}/",
        )
        return jsonify({'url': session.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/success')
def success():
    return render_template('success.html')


@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')

    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except ValueError:
            return 'Invalid payload', 400
        except stripe.error.SignatureVerificationError:
            return 'Invalid signature', 400
    else:
        try:
            event = json.loads(payload)
        except Exception:
            return 'Invalid JSON', 400

    if event.get('type') == 'checkout.session.completed':
        session_obj = event['data']['object']
        if db:
            try:
                db.table('subscriptions').insert({
                    'stripe_customer_id': session_obj.get('customer', ''),
                    'stripe_subscription_id': session_obj.get('subscription', ''),
                    'plan': 'starter',
                    'status': 'active',
                }).execute()
                print(f"Subscription recorded: {session_obj.get('subscription')}")
            except Exception as e:
                print(f"Webhook Supabase error: {e}")

    return '', 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, host='0.0.0.0', port=port)
