FROM python:3.12-slim

# System deps. cryptg is a C extension — without these, pip silently falls
# back to a pure-Python crypto path that's slower and (more importantly)
# upload less reliable for media. We install a full build toolchain so the
# wheel build succeeds even if no prebuilt wheel matches our Python/arch.
#
# libmagic1 is for the WhatsApp bridge: neonize imports python-magic, which
# needs the libmagic shared library at import time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    python3-dev \
    libssl-dev \
    libmagic1 \
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

# Same idea for the WhatsApp bridge: importing the async client loads
# neonize's bundled Go library and checks its version, and pulls in
# python-magic (libmagic). Fail the build here instead of at runtime.
RUN python -c "import neonize.aioze.client, magic; print('neonize OK')"

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
