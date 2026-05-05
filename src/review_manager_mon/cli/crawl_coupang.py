import json
import sys

from review_manager_mon.cli.args import parse_args
from review_manager_mon.coupang.runner import run_crawler


def main() -> None:
    args = parse_args()
    try:
        result = run_crawler(args)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
