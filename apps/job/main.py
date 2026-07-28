import sys

from cleanarr.cleanup import MediaCleanup


def main() -> int:
    result = MediaCleanup().run()
    return result.exit_code()


if __name__ == "__main__":
    sys.exit(main())
