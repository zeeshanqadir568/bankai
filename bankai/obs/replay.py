"""python -m bankai.obs.replay <trace_id>  ==  python -m bankai.cli replay <trace_id>"""
import sys

from ..cli import main

if __name__ == "__main__":
    sys.exit(main(["replay", *sys.argv[1:]]))
