"""python -m rehearsals {backup,production,reliability,mutation,all}"""

import argparse
import importlib
import sys
import time

REHEARSALS = {
    "backup": "rehearsals.backup_restore",
    "production": "rehearsals.production_config",
    "reliability": "rehearsals.reliability",
    "mutation": "rehearsals.mutation",
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m rehearsals",
        description="Release rehearsals against disposable data. "
                    "See docs/REHEARSALS.md.")
    parser.add_argument("names", nargs="+",
                        choices=sorted(REHEARSALS) + ["all"])
    parser.add_argument("--keep", action="store_true",
                        help="keep each temporary workspace for inspection "
                             "(it holds the run's random secrets)")
    parser.add_argument("--browser", action="store_true",
                        help="mutation only: also run the browser tests "
                             "named for each mutant (needs Playwright)")
    args = parser.parse_args(argv)

    names = list(REHEARSALS) if "all" in args.names else args.names
    results = {}
    for name in names:
        module = importlib.import_module(REHEARSALS[name])
        started = time.monotonic()
        options = {"keep": args.keep}
        if name == "mutation":
            options["browser"] = args.browser
        results[name] = (module.run(**options), time.monotonic() - started)

    print("\n=== Summary ===")
    for name, (passed, seconds) in results.items():
        print(f"{'PASS' if passed else 'FAIL'}  {name} ({seconds:.0f} s)")
    return 0 if all(passed for passed, _ in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
