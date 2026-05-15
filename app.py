import os
import uuid
import time
import threading
import tempfile
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max upload

# In-memory job store
jobs = {}

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

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


def analyze_video(job_id, video_path, mime_type):
    try:
        if not GEMINI_API_KEY:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error'] = 'GEMINI_API_KEY environment variable not set. See README for setup.'
            return

        genai.configure(api_key=GEMINI_API_KEY)

        jobs[job_id]['status'] = 'uploading'
        jobs[job_id]['step'] = 'Uploading video to Gemini...'

        video_file = genai.upload_file(video_path, mime_type=mime_type)

        jobs[job_id]['status'] = 'processing'
        jobs[job_id]['step'] = 'Gemini is processing your video...'

        # Wait for Gemini to finish processing the file
        poll_count = 0
        while video_file.state.name == "PROCESSING":
            time.sleep(3)
            poll_count += 1
            video_file = genai.get_file(video_file.name)
            if poll_count > 60:  # 3 min timeout
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

        # Clean up file from Gemini
        try:
            genai.delete_file(video_file.name)
        except Exception:
            pass

        jobs[job_id]['status'] = 'complete'
        jobs[job_id]['result'] = response.text
        jobs[job_id]['step'] = 'Done'

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = str(e)
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

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}')
    video.save(tmp.name)
    tmp.close()

    job_id = str(uuid.uuid4())
    jobs[job_id] = {'status': 'queued', 'step': 'Starting...'}

    t = threading.Thread(target=analyze_video, args=(job_id, tmp.name, mime_type), daemon=True)
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, host='0.0.0.0', port=port)
