"""Tests. Run inside the image (neonize needs Python 3.10+ and its Go library):

    docker run --rm -v "$PWD:/app" --entrypoint python telegram-forwarder:latest \
        -m unittest discover -s tests -t . -v
"""
import os
import tempfile

# Keep tests away from the real session directory.
os.environ["SESSION_DIR"] = tempfile.mkdtemp(prefix="tgf-tests-")
