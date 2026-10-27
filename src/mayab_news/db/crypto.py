"""Cifrado y descifrado de state.sqlite en reposo mediante `age` (ADR-004)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

DEFAULT_KEY_FILE = Path.home() / ".config" / "news-mayab" / "state.key"
_RECIPIENT_RE = re.compile(r"^#\s*public key:\s*(age1[a-z0-9]+)\s*$", re.MULTILINE)


def extract_public_recipient(key_content: str) -> str:
    """Extrae la clave pública (age1...) del contenido de un archivo generado por age-keygen."""
    match = _RECIPIENT_RE.search(key_content)
    if not match:
        raise ValueError(
            "No se encontró la línea '# public key: age1...' en la clave de age proporcionada."
        )
    return match.group(1)


def load_key_material(key_file: Path = DEFAULT_KEY_FILE) -> str:
    """Carga el contenido de la clave privada desde variable de entorno AGE_SECRET_KEY o archivo local."""
    env_key = os.environ.get("AGE_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    if not key_file.exists():
        raise FileNotFoundError(
            f"No existe {key_file} ni la variable de entorno AGE_SECRET_KEY."
        )
    return key_file.read_text(encoding="utf-8")


def encrypt_sqlite_file(
    plain_db_path: Path,
    encrypted_output_path: Path,
    recipient_public_key: str,
) -> None:
    """Cifra plain_db_path hacia encrypted_output_path usando el binario `age`."""
    if not plain_db_path.exists():
        raise FileNotFoundError(f"Base de datos no encontrada: {plain_db_path}")

    encrypted_output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "age",
            "--encrypt",
            "--recipient",
            recipient_public_key,
            "--output",
            str(encrypted_output_path),
            str(plain_db_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def decrypt_sqlite_file(
    encrypted_input_path: Path,
    plain_db_output_path: Path,
    key_file: Path = DEFAULT_KEY_FILE,
) -> None:
    """Descifra encrypted_input_path hacia plain_db_output_path asegurando permisos 0600."""
    if not encrypted_input_path.exists():
        raise FileNotFoundError(f"Archivo cifrado no encontrado: {encrypted_input_path}")

    plain_db_output_path.parent.mkdir(parents=True, exist_ok=True)
    key_material = load_key_material(key_file)

    subprocess.run(
        [
            "age",
            "--decrypt",
            "--identity",
            "-",
            "--output",
            str(plain_db_output_path),
            str(encrypted_input_path),
        ],
        input=key_material,
        check=True,
        capture_output=True,
        text=True,
    )
    os.chmod(plain_db_output_path, 0o600)
