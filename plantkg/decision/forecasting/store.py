"""Isolated immutable JSON artifacts, not the shared observation database."""
import json
import os
import tempfile
from pathlib import Path
from plantkg.core.contracts import ForecastBundle
from .history import digest


class ForecastStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _key(self, bundle):
        if not bundle.forecasts:
            raise ValueError("empty bundle")
        identities = []
        for f in bundle.forecasts:
            p = f.params
            if not all(p.get(k) for k in ("plant_id", "run_id", "version_manifest_id", "parameter_ref")):
                raise ValueError("forecast identity incomplete")
            identities.append([p["plant_id"], p["run_id"], p["version_manifest_id"], f.issued_at.isoformat(), f.target.model_dump(mode="json"), f.model_version, p["parameter_ref"], f.quantity])
        return digest(sorted(identities, key=lambda i: i[-1]))

    def put(self, bundle):
        key = self._key(bundle)
        payload = bundle.model_dump(mode="json")
        data = json.dumps({"format_version": 1, "content_hash": digest(payload), "bundle": payload},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        path = self.directory / (key + ".json")
        fd, temp = tempfile.mkstemp(dir=self.directory, prefix=".forecast-")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ValueError("forecast identity conflict") from None
        finally:
            os.unlink(temp)
        return key

    def get(self, key):
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("invalid forecast key")
        envelope = json.loads((self.directory / (key + ".json")).read_bytes())
        if set(envelope) != {"format_version", "content_hash", "bundle"} or envelope["format_version"] != 1 or digest(envelope["bundle"]) != envelope["content_hash"]:
            raise ValueError("forecast artifact checksum mismatch")
        bundle = ForecastBundle.model_validate(envelope["bundle"])
        if self._key(bundle) != key:
            raise ValueError("forecast artifact identity mismatch")
        return bundle
