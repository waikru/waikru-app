import os
import uuid
import time
import json
import threading
import tempfile
import secrets
from datetime import date
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import google.generativeai as genai
import stripe

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max upload
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))

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


# ---------- Auth helpers ----------

@app.context_processor
def inject_user():
    uid = session.get('user_id')
    if uid:
        return {'current_user': {'id': uid, 'email': session.get('user_email', '')}}
    return {'current_user': None}


def get_current_user():
    uid = session.get('user_id')
    if uid:
        return {'id': uid, 'email': session.get('user_email', '')}
    return None


def user_has_subscription(user_id):
    if not db or not user_id:
        return False
    try:
        res = db.table('subscriptions').select('id').eq('user_id', user_id).eq('status', 'active').limit(1).execute()
        return bool(res.data)
    except Exception:
        return False

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

BOXING_PROMPT = """You are an experienced boxing coach with 20+ years working with fighters from grassroots to professional level. Analyze this training video and give specific, technical coaching feedback.

Structure your response exactly like this:

## Overall Assessment
2-3 sentences on what you observe — skill level, energy, style, what immediately stands out.

## What's Working
Specific positives tied to what you actually see. Be concrete — name the punch, the moment, the detail that's landing well.

## Technique Breakdown
Go through what you observe and give technical notes on each:
- Stance and guard (hand height, elbow position, chin tuck, weight distribution)
- Footwork (in-and-out movement, lateral steps, pivots, balance on the exit)
- Jab (extension, shoulder roll, snap back)
- Cross (hip rotation, shoulder drive, guard hand position)
- Hook (elbow angle, hip turn, target level)
- Uppercut (knee bend, shoulder dip, guard position)
- Head movement (slipping, rolling, bobbing — what you see and when it's used)
- Defense (parrying, blocking, covering — what's there and what's missing)
- Combination rhythm and flow

## Top 3 Corrections
The three most impactful technical fixes, each explained clearly:

**1. [Issue]**
What you see → What it should look like → Why it matters

**2. [Issue]**
What you see → What it should look like → Why it matters

**3. [Issue]**
What you see → What it should look like → Why it matters

## Drills to Focus On
1-2 specific drills or exercises that directly target the corrections above.

Be direct. Talk like a corner coach, not a manual. Reference the exact punches and moments you see. If video quality or angle limits what you can assess, say so clearly."""

MMA_PROMPT = """You are an experienced MMA coach with 20+ years working with fighters across striking, wrestling, and grappling disciplines. Analyze this training video and give specific, technical coaching feedback.

Structure your response exactly like this:

## Overall Assessment
2-3 sentences on what you observe — skill level, what art they seem most comfortable in, overall athleticism and energy.

## What's Working
Specific positives tied to what you actually see in the footage. Be concrete and name the technique or moment.

## Technique Breakdown
Analyze whatever is visible in the video. Cover what applies:
- Stance and base (guard position, weight distribution, stance width)
- Striking (punches, kicks, knees, elbows — mechanics and timing)
- Head movement and defensive striking
- Clinch and dirty boxing (control, strikes in close, knee work)
- Takedown offense or defense (if visible — level changes, shot mechanics, sprawl timing)
- Ground position and control (if visible — top pressure, posture in guard, passing attempts)
- Transitions between ranges (how they move between striking, clinch, and ground)
- Combination rhythm and range management

## Top 3 Corrections
The three most impactful fixes, each explained clearly:

**1. [Issue]**
What you see → What it should look like → Why it matters in a fight context

**2. [Issue]**
What you see → What it should look like → Why it matters

**3. [Issue]**
What you see → What it should look like → Why it matters

## Drills to Focus On
1-2 specific drills or exercises that target the corrections above. Be practical — name the drill.

Be direct and technical. Talk like a coach who knows all three ranges. Reference what you specifically see happening. If the video angle or quality limits your view, say so clearly."""

BJJ_PROMPT = """You are an experienced Brazilian jiu-jitsu coach with 20+ years on the mat, working with students from white belt to black belt. Analyze this training video and give specific, technical coaching feedback.

Note: Ground grappling is harder to read from typical phone angles than stand-up striking. Be honest about what you can and cannot assess clearly from the footage available.

Structure your response exactly like this:

## Overall Assessment
2-3 sentences on what you observe — the training context (drilling, rolling, positional), approximate skill level, energy and intention on the mat.

## What's Working
Specific positives tied to what you actually see. Name the position, the movement, or the moment that stands out.

## Technique Breakdown
Go through what is visible and give technical notes on each:
- Base and posture (especially in top positions — how they carry their weight)
- Guard work (type of guard used, grips, hip activity, guard retention)
- Passing attempts (pressure, direction, weight distribution, timing)
- Submission setups (entries, control before the finish attempt, finishing mechanics)
- Sweeps and reversals (timing, grip breaks, hip movement)
- Escapes and recovery (frames, hip escape, bridge mechanics)
- Transitions between positions

## Top 3 Corrections
The three most impactful technical fixes, each explained clearly:

**1. [Issue]**
What you see → What it should look like → Why it matters on the mat

**2. [Issue]**
What you see → What it should look like → Why it matters

**3. [Issue]**
What you see → What it should look like → Why it matters

## Drills to Focus On
1-2 specific positional drills or solo movement exercises that directly address the corrections above.

Be direct and technical. Talk like a coach who has rolled thousands of rounds. Reference the exact positions and moments you observe. If the camera angle makes a specific detail hard to read, say so and explain what angle would be more useful."""

DISCIPLINE_PROMPTS = {
    'muay_thai': MUAY_THAI_PROMPT,
    'boxing': BOXING_PROMPT,
    'mma': MMA_PROMPT,
    'bjj': BJJ_PROMPT,
}

DISCIPLINE_LABELS = {
    'muay_thai': 'Muay Thai',
    'boxing': 'Boxing',
    'mma': 'MMA',
    'bjj': 'Jiu-Jitsu',
}


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


def log_analysis_start(job_id, ip_address, video_filename, video_size_mb, discipline='muay_thai', user_id=None):
    if not db:
        return
    try:
        row = {
            'id': job_id,
            'ip_address': ip_address,
            'status': 'processing',
            'video_filename': video_filename,
            'video_size_mb': round(video_size_mb, 2),
            'gemini_model': 'gemini-2.5-flash',
            'discipline': discipline,
        }
        if user_id:
            row['user_id'] = user_id
        db.table('analyses').insert(row).execute()
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

def analyze_video(job_id, video_path, mime_type, ip_address, video_filename, video_size_mb, discipline='muay_thai'):
    start_time = time.time()
    prompt = DISCIPLINE_PROMPTS.get(discipline, MUAY_THAI_PROMPT)
    try:
        if not GEMINI_API_KEY:
            raise ValueError('GEMINI_API_KEY not set.')

        genai.configure(api_key=GEMINI_API_KEY)

        jobs[job_id]['status'] = 'uploading'
        jobs[job_id]['step'] = 'Uploading your video...'

        video_file = genai.upload_file(video_path, mime_type=mime_type)

        jobs[job_id]['status'] = 'processing'
        jobs[job_id]['step'] = 'Processing your video...'

        poll_count = 0
        while video_file.state.name == "PROCESSING":
            time.sleep(3)
            poll_count += 1
            video_file = genai.get_file(video_file.name)
            if poll_count > 60:
                raise TimeoutError("Video processing timed out")

        if video_file.state.name == "FAILED":
            raise ValueError("Could not process this video file. Try a different format (MP4 works best).")

        jobs[job_id]['step'] = 'Analyzing your technique...'

        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(
            [video_file, prompt],
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


@app.route('/about')
def about():
    return render_template('about.html')


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

    # Rate limit — skip for logged-in subscribers
    ip = get_client_ip()
    user = get_current_user()
    is_subscriber = user_has_subscription(user['id']) if user else False

    if not is_subscriber and not check_daily_limit(ip):
        return jsonify({'error': 'daily_limit', 'message': "You've used your free upload for today. Upgrade to Waikru Starter for more."}), 429

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}')
    video.save(tmp.name)
    tmp.close()

    discipline = request.form.get('discipline', 'muay_thai')
    if discipline not in DISCIPLINE_PROMPTS:
        discipline = 'muay_thai'

    video_size_mb = os.path.getsize(tmp.name) / (1024 * 1024)
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status': 'queued',
        'step': 'Starting...',
        'discipline': discipline,
        'discipline_label': DISCIPLINE_LABELS.get(discipline, 'Muay Thai'),
    }

    log_analysis_start(job_id, ip, video.filename, video_size_mb, discipline, user_id=user['id'] if user else None)
    if not is_subscriber:
        increment_daily_usage(ip)

    t = threading.Thread(
        target=analyze_video,
        args=(job_id, tmp.name, mime_type, ip, video.filename, video_size_mb, discipline),
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
    return render_template('results.html', job_id=job_id, user_email=session.get('user_email'))


@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        return jsonify({'error': 'Stripe not configured.'}), 500
    try:
        host = request.host_url.rstrip('/')
        user = get_current_user()
        checkout_params = {
            'payment_method_types': ['card'],
            'line_items': [{'price': STRIPE_PRICE_ID, 'quantity': 1}],
            'mode': 'subscription',
            'success_url': f"{host}/success?session_id={{CHECKOUT_SESSION_ID}}",
            'cancel_url': f"{host}/",
        }
        if user:
            checkout_params['client_reference_id'] = user['id']
            checkout_params['customer_email'] = user['email']
        checkout_session = stripe.checkout.Session.create(**checkout_params)
        return jsonify({'url': checkout_session.url})
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
                row = {
                    'stripe_customer_id': session_obj.get('customer', ''),
                    'stripe_subscription_id': session_obj.get('subscription', ''),
                    'plan': 'starter',
                    'status': 'active',
                    'user_email': session_obj.get('customer_email', ''),
                }
                ref_id = session_obj.get('client_reference_id')
                if ref_id:
                    row['user_id'] = ref_id
                db.table('subscriptions').insert(row).execute()
                print(f"Subscription recorded: {session_obj.get('subscription')}")
            except Exception as e:
                print(f"Webhook Supabase error: {e}")

    return '', 200


# ---------- Auth routes ----------

@app.route('/login')
def login_page():
    if session.get('user_id'):
        return redirect('/dashboard')
    error = request.args.get('error')
    return render_template('login.html', error=error)


@app.route('/signup')
def signup_page():
    if session.get('user_id'):
        return redirect('/dashboard')
    error = request.args.get('error')
    return render_template('signup.html', error=error)


@app.route('/auth/login', methods=['POST'])
def auth_login():
    if not db:
        return render_template('login.html', error='Auth not configured.')
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')
    if not email or not password:
        return render_template('login.html', error='Please enter your email and password.')
    try:
        res = db.auth.sign_in_with_password({'email': email, 'password': password})
        session['user_id'] = res.user.id
        session['user_email'] = res.user.email
        session['access_token'] = res.session.access_token
        session['refresh_token'] = res.session.refresh_token
        return redirect('/dashboard')
    except Exception as e:
        return render_template('login.html', error='Invalid email or password.')


@app.route('/auth/signup', methods=['POST'])
def auth_signup():
    if not db:
        return render_template('signup.html', error='Auth not configured.')
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')
    confirm = request.form.get('confirm_password', '')
    if not email or not password:
        return render_template('signup.html', error='Please fill in all fields.')
    if password != confirm:
        return render_template('signup.html', error='Passwords do not match.')
    if len(password) < 6:
        return render_template('signup.html', error='Password must be at least 6 characters.')
    try:
        res = db.auth.sign_up({'email': email, 'password': password})
        if res.user:
            if res.session:
                session['user_id'] = res.user.id
                session['user_email'] = res.user.email
                session['access_token'] = res.session.access_token
                session['refresh_token'] = res.session.refresh_token
                return redirect('/dashboard')
            else:
                # Email confirmation required
                return render_template('signup.html', confirm_email=True, email=email)
        return render_template('signup.html', error='Signup failed. Please try again.')
    except Exception as e:
        err = str(e)
        if 'already registered' in err.lower() or 'already exists' in err.lower():
            return render_template('signup.html', error='An account with that email already exists. Try logging in.')
        return render_template('signup.html', error='Signup failed. Please try again.')


@app.route('/auth/google')
def auth_google():
    if not SUPABASE_URL:
        return redirect('/login?error=auth_not_configured')
    import urllib.parse
    callback_url = request.host_url.rstrip('/') + '/auth/callback'
    # Build the OAuth URL directly using implicit flow so tokens come back
    # in the URL hash where our callback page can read them
    oauth_url = (
        f"{SUPABASE_URL}/auth/v1/authorize"
        f"?provider=google"
        f"&redirect_to={urllib.parse.quote(callback_url, safe='')}"
    )
    return redirect(oauth_url)


@app.route('/auth/callback')
def auth_callback():
    return render_template('auth_callback.html')


@app.route('/auth/session', methods=['POST'])
def auth_session():
    data = request.json or {}
    access_token = data.get('access_token')
    refresh_token = data.get('refresh_token', '')
    if not access_token:
        return jsonify({'ok': False, 'error': 'No token provided'}), 400
    try:
        user_resp = db.auth.get_user(access_token)
        session['user_id'] = user_resp.user.id
        session['user_email'] = user_resp.user.email
        session['access_token'] = access_token
        session['refresh_token'] = refresh_token
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')


@app.route('/dashboard')
def dashboard():
    user = get_current_user()
    if not user:
        return redirect('/login')

    analyses = []
    subscription = None

    if db:
        try:
            res = db.table('analyses') \
                .select('id, discipline, status, created_at, video_filename') \
                .eq('user_id', user['id']) \
                .order('created_at', desc=True) \
                .limit(20) \
                .execute()
            analyses = res.data or []
        except Exception as e:
            print(f"Dashboard analyses error: {e}")

        try:
            res = db.table('subscriptions') \
                .select('plan, status, stripe_customer_id') \
                .eq('user_id', user['id']) \
                .eq('status', 'active') \
                .limit(1) \
                .execute()
            if res.data:
                subscription = res.data[0]
            else:
                # Fallback: try matching by email
                res2 = db.table('subscriptions') \
                    .select('plan, status, stripe_customer_id') \
                    .eq('user_email', user['email']) \
                    .eq('status', 'active') \
                    .limit(1) \
                    .execute()
                if res2.data:
                    subscription = res2.data[0]
        except Exception as e:
            print(f"Dashboard subscription error: {e}")

    return render_template('dashboard.html', user=user, analyses=analyses, subscription=subscription)


@app.route('/report/<analysis_id>')
def view_report(analysis_id):
    user = get_current_user()
    if not user:
        return redirect('/login')
    if not db:
        return "Report not available.", 404
    try:
        res = db.table('analyses') \
            .select('id, discipline, result_markdown, created_at, status') \
            .eq('id', analysis_id) \
            .eq('user_id', user['id']) \
            .limit(1) \
            .execute()
        if not res.data:
            return "Report not found.", 404
        report = res.data[0]
        if report['status'] != 'complete' or not report.get('result_markdown'):
            return "This report is not available.", 404
        discipline_label = DISCIPLINE_LABELS.get(report.get('discipline', 'muay_thai'), 'Muay Thai')
        return render_template('report.html', report=report, discipline_label=discipline_label)
    except Exception as e:
        print(f"View report error: {e}")
        return "Error loading report.", 500


@app.route('/upgrade')
def upgrade():
    user = get_current_user()
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        return redirect('/#pricingSection')
    try:
        checkout_params = {
            'mode': 'subscription',
            'line_items': [{'price': STRIPE_PRICE_ID, 'quantity': 1}],
            'success_url': request.host_url.rstrip('/') + '/success',
            'cancel_url': request.host_url.rstrip('/') + '/dashboard',
        }
        if user:
            checkout_params['client_reference_id'] = user['id']
            checkout_params['customer_email'] = user['email']
        checkout_session = stripe.checkout.Session.create(**checkout_params)
        return redirect(checkout_session.url)
    except Exception as e:
        print(f"Upgrade error: {e}")
        return redirect('/#pricingSection')


@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    user = get_current_user()
    if not user:
        return redirect('/login')
    if request.method == 'GET':
        return render_template('change_password.html')
    new_password = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')
    if not new_password or not confirm:
        return render_template('change_password.html', error='Please fill in all fields.')
    if new_password != confirm:
        return render_template('change_password.html', error='Passwords do not match.')
    if len(new_password) < 6:
        return render_template('change_password.html', error='Password must be at least 6 characters.')
    try:
        access_token = session.get('access_token')
        refresh_token = session.get('refresh_token')
        db.auth.set_session(access_token, refresh_token)
        db.auth.update_user({'password': new_password})
        return render_template('change_password.html', success=True)
    except Exception as e:
        print(f"Password change error: {e}")
        return render_template('change_password.html', error='Could not update password. Please try again.')


@app.route('/portal')
def billing_portal():
    user = get_current_user()
    if not user or not STRIPE_SECRET_KEY:
        return redirect('/dashboard')
    if not db:
        return redirect('/dashboard')
    try:
        res = db.table('subscriptions') \
            .select('stripe_customer_id') \
            .eq('user_id', user['id']) \
            .eq('status', 'active') \
            .limit(1) \
            .execute()
        if not res.data:
            return redirect('/dashboard')
        customer_id = res.data[0]['stripe_customer_id']
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=request.host_url.rstrip('/') + '/dashboard',
        )
        return redirect(portal.url)
    except Exception as e:
        print(f"Portal error: {e}")
        return redirect('/dashboard')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, host='0.0.0.0', port=port)
