FROM python:3.12-slim

# System deps. cryptg is a C extension — without these, pip silently falls
# back to a pure-Python crypto path that's slower and (more importantly)
# upload less reliable for media. We install a full build toolchain so the
# wheel build succeeds even if no prebuilt wheel matches our Python/arch.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    python3-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first to leverage layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Verify cryptg actually got installed — fail the build early if it didn't,
# rather than discovering at runtime that uploads are broken. cryptg is a
# minimal C extension and doesn't export __version__, so we just confirm
# the import works and one of its functions is callable.
RUN python -c "import cryptg; assert callable(cryptg.encrypt_ige); print('cryptg OK')"

# Application code
COPY src/ ./src/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Sessions live on a persistent volume
ENV SESSION_DIR=/data/sessions
RUN mkdir -p /data/sessions
VOLUME ["/data/sessions"]

# Make python output unbuffered so docker logs stream live
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--help"]
