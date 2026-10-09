"""Pruebas de limpieza de temporales del flujo de transcripcion."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import transcriber


class BuildAudioChunksTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleans_chunk_directory_when_ffmpeg_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            chunk_dir = Path(temp_root) / "chunks"
            with patch("transcriber._ffmpeg_is_available", return_value=True):
                with patch("transcriber.tempfile.mkdtemp", return_value=str(chunk_dir)):
                    with patch(
                        "transcriber.subprocess.run",
                        return_value=SimpleNamespace(returncode=1, stderr="invalid input"),
                    ):
                        with self.assertRaisesRegex(RuntimeError, "ffmpeg falló"):
                            await transcriber._build_audio_chunks_with_ffmpeg(
                                "input.wav", duration_seconds=1
                            )

            self.assertFalse(chunk_dir.exists())


if __name__ == "__main__":
    unittest.main()