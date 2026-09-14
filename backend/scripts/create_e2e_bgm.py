"""Create a small local MP3 suitable for the E2E fixed-BGM registration step."""

import argparse
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=Path(".e2e/bgm.mp3"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "2", "-q:a", "8", str(args.output),
        ],
        check=True,
    )
    print(args.output.resolve())


if __name__ == "__main__":
    main()
