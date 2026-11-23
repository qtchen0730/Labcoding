"""Read and update the server user's independent OpenCode output budget."""

import json
import os
from pathlib import Path
import tempfile


def opencode_config_path(home=None):
    return Path(home or Path.home()) / ".config" / "opencode" / "opencode.json"


def read_output_limit(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    return config["provider"]["ccslab"]["models"]["lab-general"]["limit"]["output"]


def write_output_limit(path, value):
    path = Path(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    config["provider"]["ccslab"]["models"]["lab-general"]["limit"]["output"] = value
    encoded = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".opencode-output-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return value
