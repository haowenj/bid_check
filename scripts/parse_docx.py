"""Parse a DOCX through MinerU and write standardized DocumentBlock output."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bid_check.mineru_client import MinerUClient, MinerUClientError
from bid_check.models import blocks_to_jsonable
from bid_check.normalizer import NormalizationError, normalize_docx_output
from bid_check.reporting import build_report, format_report, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path, help="path to a DOCX file")
    parser.add_argument("--mineru-url", default="http://127.0.0.1:7100")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--top-longest", type=int, default=5)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        artifacts = MinerUClient(args.mineru_url, args.timeout).parse_docx(
            args.docx, args.output_dir
        )
        normalized = normalize_docx_output(
            artifacts.raw_dir, artifacts.input_sha256
        )
        blocks_path = artifacts.run_dir / "document_blocks.json"
        report_path = artifacts.run_dir / "report.json"
        write_json(blocks_path, blocks_to_jsonable(normalized.blocks))
        report = build_report(normalized.blocks, args.top_longest)
        write_json(report_path, report)

        manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
        manifest["normalization"] = {
            "source_json": normalized.source_json,
            "warnings": normalized.warnings,
            "warning_count": len(normalized.warnings),
            "block_count": len(normalized.blocks),
            "document_blocks": str(blocks_path.relative_to(artifacts.run_dir)),
            "report": str(report_path.relative_to(artifacts.run_dir)),
        }
        write_json(artifacts.manifest_path, manifest)

        print(f"原始结果目录: {artifacts.raw_dir}")
        print(f"标准化后的 document_blocks.json: {blocks_path}")
        print(format_report(report))
        return 0
    except (MinerUClientError, NormalizationError, OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
