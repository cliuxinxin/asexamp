"""Fingerprint loaded source at startup, so diagnostics identify the running build."""
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def runtime_version():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    try:
        result=subprocess.run(['git','rev-parse','HEAD'],cwd=Path(__file__).resolve().parents[2],
                              capture_output=True,text=True,timeout=2,check=True)
        commit=result.stdout.strip()
    except (OSError,subprocess.SubprocessError):
        commit=None
    return {'commit':commit,'loaded_at':datetime.now(timezone.utc).isoformat(),'schema_version':1,'graph_version':3, 'protocol_version':'work-patch-v1', 'prompt_version':'incremental-v1',
            'code_fingerprint':digest.hexdigest()[:20]}
