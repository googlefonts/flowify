import argparse
from flowify import Flowify
from ufoLib2 import Font

parser = argparse.ArgumentParser(description="Turn a font into a flow font.")
parser.add_argument(
    "--slug-height",
    default="x",
    help="Height of slugs. Can be 'x' (default, x-height), 'cap' (cap height), or an integer number of units",
)
parser.add_argument(
    "--no-blank",
    action="store_true",
    help="Use rectangles instead of blanks for sequences shorter than a single slug",
)
parser.add_argument(
    "--debugging",
    action="store_true",
    help="Use an algorithm which is simpler to understand",
)
parser.add_argument(
    "--shape",
    default="pill",
    const="pill",
    nargs="?",
    choices=["pill", "rectangle"],
    help="shape of slug",
)
parser.add_argument(
    "--margin", default=20, type=int, help="Sidebearings of semicircular ends"
)
parser.add_argument(
    "--feature",
    default="rlig",
    help="OpenType feature to contain flowification lookups",
)
parser.add_argument("input", help="UFO file to convert")
parser.add_argument("output", help="Filename of new UFO")

args = parser.parse_args()
font = Font.open(args.input)

Flowify(
    font,
    args.slug_height,
    no_blank=args.no_blank,
    shape=args.shape,
    margin=args.margin,
    debugging=args.debugging,
    feature=args.feature,
)
font.save(args.output, overwrite=True)
