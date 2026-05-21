import os
import tomllib
from pathlib import Path

VALID_OCR_PROVIDERS = ("claude", "myscript")
_PLACEHOLDER_API_KEY = "YOUR_ANTHROPIC_API_KEY"


class ConfigError(Exception):
    pass


def load(path: Path = Path("config.toml")) -> dict:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    _validate(cfg, path)
    return cfg


def _validate(cfg: dict, path: Path) -> None:
    # SSH
    ssh = cfg.get("ssh", {})
    for field in ("host", "identity_file"):
        if not ssh.get(field):
            raise ConfigError(f"[ssh].{field} is required in {path}")

    identity = Path(ssh["identity_file"]).expanduser()
    if not identity.exists():
        raise ConfigError(f"[ssh].identity_file not found: {identity}")

    # Vaults
    vaults = cfg.get("vaults", [])
    if not vaults:
        raise ConfigError("At least one [[vaults]] entry is required")

    defaults = [v for v in vaults if v.get("default")]
    if len(defaults) != 1:
        raise ConfigError("Exactly one [[vaults]] entry must have default = true")

    for i, vault in enumerate(vaults):
        if not vault.get("path"):
            raise ConfigError(f"[[vaults]][{i}].path is required")
        # Path existence is verified at write time (obsidian.write_notebook_atomic)
        # rather than load time, so a vault we won't touch in this run doesn't
        # block unrelated commands.

    # OCR — required credentials depend on provider
    ocr = cfg.get("ocr", {})
    if ocr.get("enabled", False):
        provider = ocr.get("provider", "claude")
        if provider not in VALID_OCR_PROVIDERS:
            raise ConfigError(
                f"[ocr].provider must be one of {VALID_OCR_PROVIDERS}; got {provider!r}"
            )
        if provider == "myscript":
            for field in ("application_key", "hmac_key"):
                if not ocr.get(field):
                    raise ConfigError(
                        f"[ocr].{field} is required when ocr.provider = 'myscript'"
                    )
        elif provider == "claude":
            vision_key = cfg.get("vision", {}).get("api_key")
            if (not vision_key or vision_key == _PLACEHOLDER_API_KEY) and not os.environ.get(
                "ANTHROPIC_API_KEY"
            ):
                raise ConfigError(
                    "[vision].api_key (or ANTHROPIC_API_KEY env var) is required "
                    "when ocr.provider = 'claude'"
                )

    # Vision (drawing descriptions) — key required only when enabled
    vision = cfg.get("vision", {})
    if vision.get("enabled", False):
        vision_key = vision.get("api_key")
        if (not vision_key or vision_key == _PLACEHOLDER_API_KEY) and not os.environ.get(
            "ANTHROPIC_API_KEY"
        ):
            raise ConfigError(
                "[vision].api_key (or ANTHROPIC_API_KEY env var) is required "
                "when vision.enabled = true"
            )

    # Storage — bucket required only when enabled
    storage = cfg.get("storage", {})
    if storage.get("enabled", False) and not storage.get("bucket"):
        raise ConfigError("[storage].bucket is required when storage.enabled = true")

    # Daily merge — required fields and target vault must exist when enabled.
    daily = cfg.get("daily_merge", {})
    if daily.get("enabled", False):
        for field in ("source_notebook", "target_vault", "target_dir"):
            if not daily.get(field):
                raise ConfigError(
                    f"[daily_merge].{field} is required when daily_merge.enabled = true"
                )
        target_name = daily["target_vault"]
        if not any(v.get("name") == target_name for v in vaults):
            raise ConfigError(
                f"[daily_merge].target_vault {target_name!r} does not match any [[vaults]].name"
            )
