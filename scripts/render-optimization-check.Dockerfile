FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends libcairo2-dev pkg-config gcc && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pillow==11.3.0 numpy==2.4.6 pycairo==1.29.1 pydantic==2.13.5 fastapi==0.141.1 httpx==0.28.1 asyncpg==0.31.0 CairoSVG==2.9.1 pytest pytest-asyncio
WORKDIR /validation
ENV TEXTLESS_TEXT_DETECTION=false PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
