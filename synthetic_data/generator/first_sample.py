
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from synth.config import load_config
from synth.store import test_store
from synth.modules.u01_parser import ingest_path
from synth.modules.common import DocView


def main():
    document_path = ROOT / "synthetic_data" / "documents" / "harlow_note_001.txt"

    document_text = """HARLOW MARINE LIMITED

Date: 12 March 2024

Harlow Marine Limited owns Northbank Logistics Ltd.

Northbank Logistics Ltd is located in Belfast Harbour.
"""

    document_path.parent.mkdir(parents=True, exist_ok=True)

    document_path.write_text(
        document_text,
        encoding="utf-8"
    )

    print("Document created:", document_path)

    config = load_config()

    store = test_store()

    try:
        results = ingest_path(store, config, document_path)

        result = results[0]

        print("\n--- INGESTION RESULT ---")
        print("Document ID:", result["doc_id"])
        print("Status:", result["status"])
        print("Sentences:", result["sentences"])
        print("Pages:", result["pages"])

        if result["status"] != "SEGMENTED":
            raise RuntimeError(f"Ingestion failed: {result}")

        with store.reader() as reader:
            doc = DocView.load(reader, result["doc_id"], config)

        print("\n--- CANONICAL TEXT ---")
        print(doc.text)

        print("\n--- SENTENCE INDEX ---")

        for sentence in doc.sentences:
            idx = sentence["idx"]
            text = doc.sentence_text(sentence)

            print(f"s{idx}: {text}")

    finally:
        store.dispose()


if __name__ == "__main__":
    main()
