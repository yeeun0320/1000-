import io, os, zipfile, hmac, secrets
from pathlib import Path
from flask import Flask, request, render_template, send_file, jsonify, session, redirect, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
import pillow_avif

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('RENDER', '').lower() == 'true'

def authorized():
    password = os.environ.get('APP_PASSWORD', '')
    return bool(password) and session.get('authenticated') is True

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not os.environ.get('APP_PASSWORD'):
        return 'Render 환경변수 APP_PASSWORD를 먼저 설정해줘.', 503
    error = None
    if request.method == 'POST':
        submitted = request.form.get('password', '')
        if hmac.compare_digest(submitted, os.environ['APP_PASSWORD']):
            session.clear()
            session['authenticated'] = True
            return redirect(url_for('home'))
        error = '비밀번호가 올바르지 않아.'
    return render_template('login.html', error=error)

@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
ALLOWED = {'.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff','.avif'}

def number(form, key, default, lo, hi):
    try: return max(lo, min(hi, int(float(form.get(key, default)))))
    except (ValueError, TypeError): return default

def process(file, form):
    with Image.open(file) as src:
        src = ImageOps.exif_transpose(src)
        src.load()
        image = src.convert('RGBA')
    if form.get('remove_bg') == 'true':
        try:
            from rembg import remove
        except ImportError:
            raise ValueError('서버에 배경 제거 모듈이 설치되지 않았어.')
        buf = io.BytesIO(); image.save(buf, format='PNG')
        image = Image.open(io.BytesIO(remove(buf.getvalue()))).convert('RGBA')
        bbox = image.getchannel('A').getbbox()
        if bbox: image = image.crop(bbox)
    size = number(form,'canvas_size',1000,100,5000)
    if form.get('margin_mode','same') == 'individual':
        l = number(form,'margin_left',75,0,2000); t = number(form,'margin_top',75,0,2000)
        r = number(form,'margin_right',75,0,2000); b = number(form,'margin_bottom',75,0,2000)
    else:
        l=t=r=b=number(form,'margin_all',75,0,2000)
    aw, ah = max(1,size-l-r), max(1,size-t-b)
    if form.get('image_size_mode','ratio') == 'pixels':
        px = number(form,'image_pixels',850,1,5000)
        mw,mh=min(aw,px),min(ah,px)
    else:
        ratio=number(form,'image_ratio',85,1,100)/100
        mw,mh=max(1,int(aw*ratio)),max(1,int(ah*ratio))
    scale=min(mw/image.width,mh/image.height)
    w,h=max(1,int(image.width*scale)),max(1,int(image.height*scale))
    resized=image.resize((w,h),Image.Resampling.LANCZOS)
    result=Image.new('RGBA',(size,size),'white')
    x=l+(aw-w)//2+number(form,'offset_x',0,-1000,1000)
    y=t+(ah-h)//2+number(form,'offset_y',0,-1000,1000)
    # paste with clipping for large offsets
    result.paste(resized,(x,y),resized)
    fmt=form.get('output_format','PNG').upper()
    if fmt not in ('PNG','JPG'): fmt='PNG'
    output=io.BytesIO()
    if fmt=='JPG':
        result.convert('RGB').save(output,'JPEG',quality=number(form,'jpeg_quality',95,50,100),optimize=True)
    else: result.save(output,'PNG',optimize=True)
    output.seek(0)
    return output,fmt

@app.get('/')
def home():
    if not authorized(): return redirect(url_for('login'))
    return render_template('index.html')

@app.post('/convert')
def convert():
    if not authorized(): return jsonify(error='비밀번호 인증이 필요해.'), 401
    files=[f for f in request.files.getlist('files') if f.filename]
    if not files: return jsonify(error='이미지를 먼저 추가해줘.'),400
    if len(files)>100: return jsonify(error='한 번에 최대 100장까지 변환할 수 있어.'),400
    preview=request.form.get('preview')=='true'
    if preview: files=files[:1]
    out=io.BytesIO(); errors=[]; successes=0
    with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        used=set()
        for f in files:
            try:
                name=Path(f.filename).name
                if Path(name).suffix.lower() not in ALLOWED: raise ValueError('지원하지 않는 파일 형식')
                data,fmt=process(f.stream,request.form)
                if preview:
                    return send_file(data,mimetype='image/jpeg' if fmt=='JPG' else 'image/png',download_name='preview.'+('jpg' if fmt=='JPG' else 'png'))
                ext='jpg' if fmt=='JPG' else 'png'
                base=f'{Path(name).stem}_1000x1000'; target=f'{base}.{ext}'; n=2
                while target in used:
                    target=f'{base}_{n}.{ext}'; n+=1
                used.add(target); archive.writestr(target,data.getvalue()); successes+=1
            except Exception as e: errors.append(f'{f.filename}: {e}')
        if errors: archive.writestr('변환_오류.txt','\n'.join(errors))
    if successes==0: return jsonify(error='변환에 실패했어. '+'; '.join(errors[:3])),400
    out.seek(0)
    return send_file(out,mimetype='application/zip',as_attachment=True,download_name='converted_images.zip')

@app.errorhandler(413)
def too_large(e): return jsonify(error='업로드 용량이 100MB를 초과했어.'),413

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)
