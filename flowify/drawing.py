# This part of the code is all about drawing the glyphs we need.

# Length of cubic Bezier handle used when drawing quarter circles.
# See https://pomax.github.io/bezierinfo/#circles_cubic
CIRCULAR_SUPERNESS = 0.551784777779014

# A routine to draw open / closing semicircles into a glyph,
# with some padding on the left hand side for the closing semicircles
def draw_semicircle(glyph, circumference, left=True, margin=20):
    radius = circumference / 2
    if left:
        origin = (circumference / 2, circumference / 2)
    else:
        origin = (0, circumference / 2)

    pen = glyph.getPen()
    w = (origin[0] - radius + margin, origin[1])
    n = (origin[0], origin[1] + radius)
    e = (origin[0] + radius - margin, origin[1])
    s = (origin[0], origin[1] - radius)
    pen.moveTo(s)
    if left:
        pen.curveTo(
            (s[0] - radius * CIRCULAR_SUPERNESS, s[1]),
            (w[0], w[1] - radius * CIRCULAR_SUPERNESS),
            w,
        )
        pen.curveTo(
            (w[0], w[1] + radius * CIRCULAR_SUPERNESS),
            (n[0] - radius * CIRCULAR_SUPERNESS, n[1]),
            n,
        )
    else:
        pen.lineTo(n)
        pen.curveTo(
            (n[0] + radius * CIRCULAR_SUPERNESS, n[1]),
            (e[0], e[1] + radius * CIRCULAR_SUPERNESS),
            e,
        )
        pen.curveTo(
            (e[0], e[1] - radius * CIRCULAR_SUPERNESS),
            (s[0] + radius * CIRCULAR_SUPERNESS, s[1]),
            s,
        )
    pen.closePath()
    glyph.width = circumference / 2
    if left:
        glyph.setLeftMargin(margin)
    else:
        glyph.setRightMargin(margin)
    assert glyph.width == circumference / 2


# Routine to draw a rectangle
def draw_slug(glyph, width, height):
    pen = glyph.getPen()
    pen.moveTo((0, 0))
    pen.lineTo((width, 0))
    pen.lineTo((width, height))
    pen.lineTo((0, height))
    pen.closePath()

