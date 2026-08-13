FROM python:3.12-slim

WORKDIR /app

# Install necessary build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    adb \
    && rm -rf /var/lib/apt/lists/*

# Copy application source code and apps
COPY artemis/ /app/artemis/
COPY apps/ /app/apps/
COPY config/ /app/config/
COPY pyproject.toml setup.py README.md LICENSE /app/

# Install server and core dependencies
RUN pip install --no-cache-dir \
    uvicorn \
    fastapi \
    pydantic \
    pydantic-settings \
    python-dotenv \
    sseclient-py \
    websockets \
    adbutils \
    jinja2 \
    colorama \
    psutil \
    httpx \
    retry \
    google-auth \
    langchain \
    langchain-core \
    langchain-community \
    langchain-google-genai \
    langchain-google-vertexai \
    langchain-mcp-adapters \
    google-genai \
    langgraph \
    typer \
    pillow \
    packaging \
    uiautomator2 \
    opencv-python-headless \
    uuid-utils \
    posthog \
    mcp \
    inquirer \
    rich \
    requests \
    imageio-ffmpeg \
    langchain-openai

# Install artemis package locally (no-deps to prevent pulling unnecessary heavy optional libraries)
RUN pip install --no-cache-dir --no-deps -e /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV ARTEMIS_APP_DIR=/app
ENV PORT=8080

EXPOSE 8080

CMD ["uvicorn", "apps.admin_console.server:app", "--host", "0.0.0.0", "--port", "8080"]
