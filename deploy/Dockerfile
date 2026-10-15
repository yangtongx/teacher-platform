FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 teacher \
    && useradd --system --uid 10001 --gid 10001 --home-dir /home/teacher --create-home --shell /usr/sbin/nologin teacher

WORKDIR /srv/teacher-platform
COPY requirements.txt .
# 构建时下载版面、中文识别和英文行识别模型，上传图片时不必再联网。
ENV HF_ENDPOINT=https://hf-mirror.com
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    -r requirements.txt \
    && python -c "from rapidocr import RapidOCR; from rapid_layout import RapidLayout; RapidOCR(); RapidLayout()" \
    && python -c "from transformers import TrOCRProcessor, VisionEncoderDecoderModel; name='microsoft/trocr-base-printed'; TrOCRProcessor.from_pretrained(name); VisionEncoderDecoderModel.from_pretrained(name)" \
    && if [ -d /root/.cache ]; then cp -a /root/.cache /home/teacher/.cache; fi \
    && chown -R teacher:teacher /home/teacher
COPY app ./app
COPY static ./static
COPY workspaces ./workspaces
COPY ppt_templates ./ppt_templates

USER 10001:10001
EXPOSE 18900
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18900/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18900"]
