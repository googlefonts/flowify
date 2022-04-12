from ufoLib2.objects import Glyph
from fontFeatures import FontFeatures, Substitution, Routine, Chaining
import numpy as np
import inflect
from ufo2ft.filters import BaseFilter
from ufo2ft.util import _GlyphSet, _LazyFontName
import flowify.drawing
import logging

logger = logging.getLogger(__name__)


class Flowify:
    def __init__(
        self,
        font,
        slug_height="x",
        no_blank=False,
        shape="pill",
        feature="rlig",
        margin=20,
        debugging=False,
        max_kern_rules_per_lookup=20,
    ):
        self.font = font
        self.max_kern_rules_per_lookup = max_kern_rules_per_lookup
        self.kern_rules = {}
        self.adder_place_cache = {}

        if slug_height == "x":
            self.slug_height = font.info.xHeight
        elif slug_height == "cap":
            self.slug_height = font.info.capHeight
        else:
            self.slug_height = int(slug_height)

        self.ff = FontFeatures()

        if debugging:
            # Base 10 is much easier to understand! Use it for a debugging build
            self.PLACES = 4
            self.BASE = 10
        else:
            # BASE ** PLACES can't go over 32767, so this is as close as we can get
            # to the actual max advance width
            self.PLACES = 7
            self.BASE = 4

        self.encoded_slug_height = self.encode(self.slug_height)
        self.actual_glyphs = [
            g.name
            for g in font
            if g.name not in font.lib.get("public.skipExportGlyphs", {})
        ]

        self.setup_needed_glyphs(margin)
        self.relevant_glyphs = []
        for g in self.actual_glyphs:
            if (
                g == "space"
                or g.startswith("slug")
                or g.startswith("_")
                or not self.font[g].width
            ):
                continue
            if g == ".notdef":
                continue
            self.relevant_glyphs.append(g)

        if debugging:
            self.add_debugging_glyphs()
        self.create_some_routines()
        self.add_feature(feature, no_blank, shape)

        font.info.styleName += " Flow"

    def setup_needed_glyphs(self, margin):

        # Add the left and right ends
        sc_l = Glyph("slug.left")
        drawing.draw_semicircle(sc_l, self.slug_height, True, margin=margin)
        self.font.addGlyph(sc_l)

        sc_r = Glyph("slug.right")
        drawing.draw_semicircle(sc_r, self.slug_height, False, margin=margin)
        self.font.addGlyph(sc_r)

        self.added_glyphs = ["slug.left", "slug.right"]

        # We need three different kinds of place glyph:
        #  * _w.XeY holds an intermediate computation: the width of a glyph or partial sum
        #    This needs to be a mark glyph so we can filter on it.
        #  * _W.XEY represents a final computation, the total width of the word. It is
        #    a base glyph and contains an outline as wide as its value.
        #  * _W.XEY.blank is a blanked-out spacer used when the total width is less than
        #    the size of the circle. Again its width is the same as its value.
        # We will also have a carry glyph for each place

        self.calculation_glyphs = []  # All the intermediate computations
        self.carries = []  # All the carries
        self.w_e = []  # All the intermediate computation glyphs for a given place
        counter = 0
        for exponent in range(self.PLACES):
            values = []
            for value in range(self.BASE):
                counter += 1
                decimal = value
                if exponent > 0:
                    decimal *= self.BASE ** exponent
                # Intermediate computation glyph _w.1e1
                gname = "_w.%ie%i" % (value, exponent)
                values.append(gname)
                g = Glyph(gname)
                self.font.addGlyph(g)
                self.added_glyphs.append(gname)
                self.ff.glyphclasses[gname] = "mark"

                # Result slug _W.1e1
                g = Glyph(gname.upper(), width=decimal)
                drawing.draw_slug(g, decimal, self.slug_height)
                self.font.addGlyph(g)
                self.added_glyphs.append(gname.upper())
                self.ff.glyphclasses[gname.upper()] = "base"

                # Blank result slug
                g = Glyph(gname.upper() + ".blank", width=decimal)
                self.font.addGlyph(g)
                self.added_glyphs.append(gname.upper() + ".blank")
                self.ff.glyphclasses[gname.upper() + ".blank"] = "base"
            self.w_e.append(values)
            self.font.addGlyph(Glyph("_carry.e%i" % exponent))
            self.added_glyphs.append("_carry.e%i" % exponent)
            self.ff.glyphclasses["_carry.e%i" % exponent] = "mark"
            self.carries.append("_carry.e%i" % exponent)
            self.calculation_glyphs.extend(values)

        # We also need mark glyphs to mark the start and end of words
        for g in ["_start", "_end"]:
            self.font.addGlyph(Glyph(g))
            self.ff.glyphclasses[g] = "mark"
            self.added_glyphs.append(g)

    # If debugging, you get 50 glyphs in the PUA to play with widths directly.
    def add_debugging_glyphs(self):
        p = inflect.engine()
        for r in range(50):
            gname = "w." + p.number_to_words(r)
            self.font.addGlyph(Glyph(name=gname, width=r, unicodes=[0xE000 + r]))
            self.added_glyphs.append(gname)

    # Turn a glyph into a sequence of glyphs representing its length.
    # i.e. in debugging mode, "a" with width 553 becomes _w.3e0 _w.5e1 _w.5e2 _w.0e3
    # read it backwards: 0553.
    def encode(self, width):
        if width < 0:
            width = self.BASE ** self.PLACES + width
        glyphs = []
        for exponent, value in enumerate(
            reversed(np.base_repr(int(width), base=self.BASE).zfill(self.PLACES))
        ):
            glyphs.append(["_w.%se%i" % (value, exponent)])
        return glyphs

    # Kerning will be handed by inserting another glyph-sequence into the sum.
    # A positive kern is easy: kern 10 units just means _w.0e0 _w.1e1 _w.0e2 _w.0e3
    # Negative kerning uses complements, and because of rollover, the addition just works.
    # -10 = _w.0e0 _w.9e1 _w.9e2 _w.9e3
    # We have to express each kern rule as "sub left' lookup kernminus10 right"
    # We will build the coverage / substitution for each of the "kernXX" lookups
    # dynamically; when we add "sub [a b]' lookup kernminus10 y" to the kern table,
    # we grab our cached "kernminus10" and then ensure that it includes coverage for
    # glyphs "a" and "b".
    def kern_rule_for(self, l, value):
        if value not in self.kern_rules:
            self.kern_rules[value] = Routine(
                name="kern_%s" % (str(value).replace("-", "minus")),
                rules=[Substitution([set()], replacement=[set()] + self.encode(value))],
            )
        self.kern_rules[value].rules[0].replacement[0] = list(
            set(self.kern_rules[value].rules[0].input[0]) | set(l)
        )
        self.kern_rules[value].rules[0].input[0] = list(
            set(self.kern_rules[value].rules[0].input[0]) | set(l)
        )

        return self.kern_rules[value]

    # Now the actual lookups!

    def create_some_routines(self):
        # Attached mark glyphs get in the way, so we remove them early.
        marks = [
            g
            for g in self.actual_glyphs
            if self.font.lib.get("public.openTypeCategories", {}).get(g, "") == "mark"
            or self.font[g].width == 0
        ]
        self.delete_marks = Routine(
            name="delete_marks", rules=[Substitution([marks], [])]
        )

        # Routines to add marker glyphs at start and end
        self.add_start = Routine(
            name="add_start",
            flags=0x8,
            rules=[
                Chaining(
                    [self.relevant_glyphs],
                    precontext=[self.relevant_glyphs],
                    lookups=[[]],
                ),
                Chaining(
                    [self.relevant_glyphs],
                    lookups=[
                        [
                            Routine(
                                name="do_add_start",
                                rules=[
                                    Substitution(
                                        [self.relevant_glyphs],
                                        [["_start"], self.relevant_glyphs],
                                    )
                                ],
                            )
                        ]
                    ],
                ),
            ],
        )

        # When adding the end marker glyph, we will also add a set of zeros to hold
        # the final computation. Adding a zero at the end means the last digit's
        # carries are processed.
        self.add_end = Routine(
            name="add_end",
            flags=0x8,
            rules=[
                Chaining(
                    [self.relevant_glyphs],
                    postcontext=[self.relevant_glyphs],
                    lookups=[[]],
                ),
                Chaining(
                    [self.relevant_glyphs],
                    lookups=[
                        [
                            Routine(
                                name="do_add_end",
                                rules=[
                                    Substitution(
                                        [self.relevant_glyphs],
                                        [self.relevant_glyphs]
                                        + self.encode(0)
                                        + [["_end"]],
                                        # [relevant_glyphs] + [["_end"]],
                                    )
                                ],
                            )
                        ]
                    ],
                ),
            ],
        )

        # These are the substitutions which turn each glyph into the encoded
        # form of its advance width
        self.subrules = Routine(name="encode")
        for g in self.relevant_glyphs:
            self.subrules.rules.append(
                Substitution([[g]], self.encode(self.font[g].width))
            )

        # Replace the intermediate glyphs with upper-case versions to form the slug.
        # We will contextually apply this only to the rightmost number in the sequence
        # (i.e. the overall total).
        self.do_record_result = Routine(
            name="do_record_result",
            rules=[
                Substitution(
                    [self.calculation_glyphs],
                    [[x.upper() for x in self.calculation_glyphs]],
                )
            ],
        )

        # Routine to tidy up an interim calculation. Will be applied contextually
        # after we've done the sum
        self.do_delete = Routine(
            name="delete",
            rules=[Substitution([self.calculation_glyphs + self.carries], [])],
        )

        # Interim calculations get deleted but final calculations get uppercased
        # to form the slug whose width is the sum of the advance widths in
        # this sequence.
        self.record_result = Routine(
            name="record_result",
            markFilteringSet=self.calculation_glyphs + ["_end"],
            rules=[
                Chaining(
                    self.w_e,
                    postcontext=[["_end"]],  # This is the last one
                    lookups=[[self.do_record_result]] * self.PLACES,
                ),
                Chaining(
                    [self.calculation_glyphs + self.carries], lookups=[[self.do_delete]]
                ),
            ],
        )

        # A routine to insert the starting semicircle after the start
        # marker glyph
        self.insert_start_slug = Routine(
            name="insert_start_slug",
            rules=[Substitution([["_start"]], [["slug.left"], ["_start"]])],
        )

        # A routine to blank out words whose advance width is < slug_height
        self.do_blank = Routine(
            name="do_blank",
            rules=[
                Substitution(
                    [self.calculation_glyphs],
                    [[x.upper() + ".blank" for x in self.calculation_glyphs]],
                )
            ],
        )

        # I just like to be tidy.
        self.delete_carries = Routine(
            name="delete_carries",
            rules=[Substitution([self.carries], [])],
        )
        self.delete_rubbish = Routine(
            name="delete_rubbish",
            rules=[Substitution([["_start", "_end"]], [])],
        )

    # Trickier lookups require their own methods! Let's start with kerning.
    # The aim of the game is to convert each kerning rule into a substitution rule.
    # Unfortunately we can't pack these contextual substitutions as efficiently
    # as a class-based pair positioning lookup, so we have to split the rule every
    # so often to stop it overflowing.
    def make_kerning_routines(self):
        kerning_routines = []
        kerning = None
        for (l, r), value in self.font.kerning.items():
            if not kerning:
                kerning = Routine(name="slug_kerning_%i" % len(kerning_routines))
            l = self.font.groups.get(l, [l])
            r = self.font.groups.get(r, [r])
            l = [
                x
                for x in l
                if x not in self.font.lib.get("public.skipExportGlyphs", {})
                and x in self.relevant_glyphs
            ]
            r = [
                x
                for x in r
                if x not in self.font.lib.get("public.skipExportGlyphs", {})
                and x in self.relevant_glyphs
            ]
            if l and r:
                kerning.rules.append(
                    Chaining(
                        [l],
                        postcontext=[r],
                        lookups=[[self.kern_rule_for(l, value)], []],
                    )
                )
            if len(kerning.rules) > self.max_kern_rules_per_lookup:
                kerning_routines.append(kerning)
                kerning = None
        if kerning:
            kerning_routines.append(kerning)
        return kerning_routines

    # Now we build the adder routine. Probably best to look at the output
    # feature code to understand what this is doing.
    def _make_an_adder_for_place(self, exponent):
        value = self.BASE ** exponent

        def make_add_routine(add):
            name = "add_%i" % (add * value)
            if add == self.BASE:
                name += "_c"
            try:
                return self.ff.referenceRoutine(self.ff.routineNamed(name))
            except ValueError:
                pass
            rules = []
            for before in range(0, self.BASE):
                after = before + add
                if after >= self.BASE:
                    after = after % self.BASE
                    if exponent != (self.PLACES - 1):
                        rules.append(
                            Substitution(
                                [["_w.%ie%i" % (before, exponent)]],
                                [
                                    ["_w.%ie%i" % (after, exponent)],
                                    ["_carry.e%i" % (exponent + 1)],
                                ],
                            )
                        )
                        continue
                    # Don't carry if this is the last digit; fall through
                rules.append(
                    Substitution(
                        [["_w.%ie%i" % (before, exponent)]],
                        [["_w.%ie%i" % (after, exponent)]],
                    )
                )
            return self.ff.referenceRoutine(Routine(name=name, rules=rules))

        routines = [make_add_routine(i) for i in range(0, self.BASE + 1)]
        # We don't actually need an "add zero" but it makes the list indices match up

        if exponent == 0:
            if 0 not in self.adder_place_cache:
                self.adder_place_cache[0] = Routine(
                    name="adder_place_%i" % exponent,
                    flags=0x10,
                    markFilteringSet=["_start", "_end"] + self.w_e[0],
                )
                for i in range(1, self.BASE):
                    self.adder_place_cache[0].rules.append(
                        Chaining(
                            [["_w.%ie0" % i], self.w_e[0]],
                            lookups=([[], [routines[i]]]),
                        )
                    )
            return Routine(
                name="adder0",
                rules=[
                    Chaining([self.w_e[0]], lookups=[[self.adder_place_cache[0]]]),
                ],
            )
        else:
            if exponent not in self.adder_place_cache:
                self.adder_place_cache[exponent] = Routine(
                    flags=0x10,
                    name="adder_place_%i" % exponent,
                    markFilteringSet=["_start", "_end", "_carry.e%i" % exponent]
                    + self.w_e[exponent],
                )
                # Add carry rules first
                for i in range(0, self.BASE):
                    self.adder_place_cache[exponent].rules.append(
                        Chaining(
                            [
                                ["_w.%ie%i" % (i, exponent)],
                                ["_carry.e%i" % exponent],
                                self.w_e[exponent],
                            ],
                            lookups=[[], [], [routines[i + 1]]],
                        ),
                    )
                for i in range(1, self.BASE):
                    self.adder_place_cache[exponent].rules.append(
                        Chaining(
                            [["_w.%ie%i" % (i, exponent)], self.w_e[exponent]],
                            lookups=[[], [routines[i]]],
                        )
                    )
            return Routine(
                name="adder%i" % exponent,
                markFilteringSet=self.calculation_glyphs,
                rules=[
                    Chaining(
                        [self.w_e[exponent]],
                        lookups=[[self.adder_place_cache[exponent]]],
                    ),
                ],
            )

    def make_an_adder(self, generation):
        place_adders = [self._make_an_adder_for_place(i) for i in range(0, self.PLACES)]
        for p in place_adders:
            p.name = p.name + "_%i" % generation
        return place_adders

    # Is the length > slug_height?
    # If so, negate slug height from it and replace start and end with curves.

    # How do we test if something is bigger than the slug height?
    # Let's say the slug height is 1 2 3 4.
    # The following patterns will be bigger:
    #  [23456789] anything  anything anything.
    #  1          [3456789] anything anything.
    #  1          2         [456789] anything.
    #  1          2         3        [56789]
    # The nested for loop below does that, but backwards because our numbers are
    # encoded right to left.
    def test_if_length_is_more_than_slug_height(self):
        # fill() turns "3" into "3456789" in the example above
        def fill(glyph):
            value, exponent = int(glyph[-3]), int(glyph[-1])
            if value == self.BASE - 1:
                return []
            return self.w_e[exponent][value + 1 :]

        patterns_bigger_than_slug_height = []
        for pointer in range(self.PLACES - 1, -1, -1):
            pattern = []
            for place in range(self.PLACES - 1, -1, -1):
                if place > pointer:
                    pattern.append(self.encoded_slug_height[place])
                elif place == pointer:
                    pattern.append(fill(self.encoded_slug_height[place][0]))
                else:
                    pattern.append(self.w_e[place])
            if all(pattern):
                patterns_bigger_than_slug_height.append(list(reversed(pattern)))
        return patterns_bigger_than_slug_height

    def add_slugs(self, no_blank=False):
        # This deletes the intermediate sums apart from the final total, without
        # upper-casing the glyphs to form the slug. It just makes the comparison step
        # a little easier to follow in the debugger.
        tidy_result = Routine(
            name="tidy_result",
            markFilteringSet=self.calculation_glyphs + ["_end"] + self.carries,
            rules=[
                Chaining(
                    self.w_e,
                    postcontext=[["_end"]],
                    lookups=[[]] * self.PLACES,  # This is the last one
                ),
                Chaining(
                    [self.calculation_glyphs + self.carries], lookups=[[self.do_delete]]
                ),
            ],
        )

        # After we've inserted the two semicircles, our total slug is now bigger than it
        # should be. So we need to take away the length of the semicircle. To do this, we
        # insert a negative kern the size of the slug height, and we'll have another round
        # with the adder to find the final size.
        insert_negative_and_end_slug = Routine(
            name="insert_negative_and_end_slug",
            rules=[
                Substitution(
                    [["_end"]],
                    self.encode(-self.slug_height) + [["_end"], ["slug.right"]],
                )
            ],
        )

        patterns_bigger_than_slug_height = (
            self.test_if_length_is_more_than_slug_height()
        )

        # We add the bits at the end first. We can't do it in one go because inserting a
        # starting semicircle causes the glyph stream length to change, and our final
        # lookup falls off the end.
        compare1 = Routine(
            name="compare1",
            rules=[],
        )
        for p in patterns_bigger_than_slug_height:
            compare1.rules.append(
                Chaining(
                    [["_start"]] + p + [["_end"]],
                    lookups=[[]] * (1 + self.PLACES) + [[insert_negative_and_end_slug]],
                )
            )
        compare2 = Routine(
            name="compare2",
            rules=[],
        )
        for p in patterns_bigger_than_slug_height:
            compare2.rules.append(
                Chaining(
                    [["_start"]] + p,
                    lookups=[[self.insert_start_slug]] + [[]] * self.PLACES,
                )
            )
        # Here we blank out things which are not bigger than the slug height.
        if not no_blank:
            compare2.rules.append(
                Chaining(
                    [["_start"]] + self.w_e + [["_end"]],
                    lookups=([[]] + [[self.do_blank]] * self.PLACES + [[]]),
                ),
            )
        return [tidy_result, compare1, compare2]

    def add_feature(self, feature, no_blank=False, shape="pill"):
        if shape == "pill":
            stage2 = self.add_slugs(no_blank) + self.make_an_adder(2)
        else:
            stage2 = []
        # Put it all together
        self.ff.addFeature(
            feature,
            [self.add_start, self.add_end, self.delete_marks]
            + self.make_kerning_routines()
            + [self.subrules]
            + self.make_an_adder(1)
            + stage2
            + [self.delete_carries, self.record_result, self.delete_rubbish],
        )

        # Add our features to the end of the feature file
        self.font.features.text += self.ff.asFea()


class FlowifyFilter(BaseFilter):

    _kwargs = {
        "margin": 20,
        "slug_height": "x",
        "no_blank": False,
        "shape": "pill",
        "feature": "rlig",
        "max_kern_rules_per_lookup": 20,
        "debugging": False,
    }

    def __call__(self, font, glyphSet=None):
        fontName = _LazyFontName(font)
        if glyphSet is not None and getattr(glyphSet, "name", None):
            logger.info("Running %s on %s-%s", self.name, fontName, glyphSet.name)
        else:
            logger.info("Running %s on %s", self.name, fontName)

        if glyphSet is None:
            glyphSet = _GlyphSet.from_layer(font)

        self.set_context(font, glyphSet)
        f = Flowify(
            font,
            slug_height=self.options.slug_height,
            no_blank=self.options.no_blank,
            shape=self.options.shape,
            feature=self.options.feature,
            margin=self.options.margin,
            debugging=self.options.debugging,
            max_kern_rules_per_lookup=self.options.max_kern_rules_per_lookup,
        )
        for g in f.added_glyphs:
            glyphSet[g] = font[g]
        return f.relevant_glyphs + f.added_glyphs
