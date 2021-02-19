#  Copyright (c) 2021, Manfred Moitzi
#  License: MIT License
#  All tools in this module have to be independent from DXF entities!
from typing import (
    List, Iterable, Tuple, TYPE_CHECKING, Union, Optional, Callable,
)
import re
import math
from ezdxf.lldxf import validator, const
from ezdxf.lldxf.const import SPECIAL_CHARS_ENCODING
from ezdxf.math import Vec3, BoundingBox, Matrix44
from .fonts import FontMeasurements, AbstractFont

if TYPE_CHECKING:
    from ezdxf.eztypes import Text, MText, DXFEntity

LEFT = 0
CENTER = 1
RIGHT = 2

BASELINE = 0
BOTTOM = 1
MIDDLE = 2
TOP = 3
X_MIDDLE = 4  # special case for overall alignment "MIDDLE"

MTEXT_ALIGN_FLAGS = {
    1: (LEFT, TOP),
    2: (CENTER, TOP),
    3: (RIGHT, TOP),
    4: (LEFT, MIDDLE),
    5: (CENTER, MIDDLE),
    6: (RIGHT, MIDDLE),
    7: (LEFT, BOTTOM),
    8: (CENTER, BOTTOM),
    9: (RIGHT, BOTTOM),
}


class TextLine:
    def __init__(self, text: str, font: 'AbstractFont'):
        self._font = font
        self._text_width: float = font.text_width(text)
        self._stretch_x: float = 1.0
        self._stretch_y: float = 1.0

    def stretch(self, alignment: str, p1: Vec3, p2: Vec3) -> None:
        sx: float = 1.0
        sy: float = 1.0
        if alignment in ('FIT', 'ALIGNED'):
            defined_length: float = (p2 - p1).magnitude
            if self._text_width > 1e-9:
                sx = defined_length / self._text_width
                if alignment == 'ALIGNED':
                    sy = sx
        self._stretch_x = sx
        self._stretch_y = sy

    @property
    def width(self) -> float:
        return self._text_width * self._stretch_x

    @property
    def height(self) -> float:
        return self._font.measurements.total_height * self._stretch_y

    def font_measurements(self) -> FontMeasurements:
        return self._font.measurements.scale(self._stretch_y)

    def baseline_vertices(self, insert: Vec3, halign: int = 0, valign: int = 0,
                          angle: float = 0, scale=(1, 1, 1)) -> List[Vec3]:
        """ Returns the left and the right baseline vertex of the text line.

        Args:
            insert: insertion point
            halign: horizontal alignment left=0, center=1, right=2
            valign: vertical alignment baseline=0, bottom=1, middle=2, top=3
            angle: text rotation in radians
            scale: scale in x-, y and z-axis as 3-tuple of float

        """
        fm = self.font_measurements()
        vertices = [
            Vec3(0, fm.baseline),
            Vec3(self.width, fm.baseline),
        ]
        shift = self._shift_vector(halign, valign, fm)
        return _transform(vertices, insert, shift, angle, scale)

    def corner_vertices(self, insert: Vec3, halign: int = 0, valign: int = 0,
                        angle: float = 0, scale=(1, 1, 1)) -> List[Vec3]:
        """ Returns the corner vertices of the text line in the order
        bottom left, bottom right, top right, top left.

        Args:
            insert: insertion point
            halign: horizontal alignment left=0, center=1, right=2
            valign: vertical alignment baseline=0, bottom=1, middle=2, top=3
            angle: text rotation in radians
            scale: scale in x-, y and z-axis as 3-tuple of float

        """
        fm = self.font_measurements()
        vertices = [
            Vec3(0, fm.bottom),
            Vec3(self.width, fm.bottom),
            Vec3(self.width, fm.cap_top),
            Vec3(0, fm.cap_top),
        ]
        shift = self._shift_vector(halign, valign, fm)
        return _transform(vertices, insert, shift, angle, scale)

    def _shift_vector(self, halign: int, valign: int,
                      fm: FontMeasurements) -> Vec3:
        return Vec3(
            _shift_x(self.width, halign),
            _shift_y(fm, valign)
        )


def _transform(vertices: Iterable[Vec3],
               insert: Vec3,
               shift: Vec3,
               rotation: float,
               scale: Tuple[float, float, float],
               ) -> List[Vec3]:
    shift_x = shift.x
    shift_y = shift.y
    shift_z = shift.z
    sx, sy, sz = scale
    vertices = (
        Vec3(
            (v.x + shift_x) * sx,
            (v.y + shift_y) * sy,
            (v.z + shift_z) * sz,
        ) for v in vertices
    )
    return [insert + v.rotate(rotation) for v in vertices]


def text_transformation(fm: FontMeasurements, bbox: BoundingBox,
                        align: str, length: float) -> Matrix44:
    halign, valign = const.TEXT_ALIGN_FLAGS[align.upper()]
    matrix = alignment_transformation(fm, bbox, halign, valign)

    stretch_x = 1.0
    stretch_y = 1.0
    if align == 'ALIGNED':
        stretch_x = length / bbox.size.x
        stretch_y = stretch_x
    elif align == 'FIT':
        stretch_x = length / bbox.size.x
    if stretch_x != 1.0:
        matrix *= Matrix44.scale(stretch_x, stretch_y, 1.0)
    return matrix


def alignment_transformation(fm: FontMeasurements, bbox: BoundingBox,
                             halign: int, valign: int) -> Matrix44:
    if halign == const.LEFT:
        shift_x = 0
    elif halign == const.RIGHT:
        shift_x = -bbox.extmax.x
    elif halign == const.CENTER or halign > 2:  # ALIGNED, MIDDLE, FIT
        shift_x = -bbox.center.x
    else:
        raise ValueError(f'invalid halign argument: {halign}')
    cap_height = fm.cap_height
    descender_height = fm.descender_height
    if valign == const.BASELINE:
        shift_y = 0
    elif valign == const.TOP:
        shift_y = -cap_height
    elif valign == const.MIDDLE:
        shift_y = -cap_height / 2
    elif valign == const.BOTTOM:
        shift_y = descender_height
    else:
        raise ValueError(f'invalid valign argument: {valign}')
    if halign == 4:  # MIDDLE
        shift_y = -cap_height + fm.total_height / 2.0
    return Matrix44.translate(shift_x, shift_y, 0)


def _shift_x(total_width: float, halign: int) -> float:
    if halign == CENTER:
        return -total_width / 2.0
    elif halign == RIGHT:
        return -total_width
    return 0.0  # LEFT


def _shift_y(fm: FontMeasurements, valign: int) -> float:
    if valign == BASELINE:
        return fm.baseline
    elif valign == MIDDLE:
        return -fm.cap_top + fm.cap_height / 2
    elif valign == X_MIDDLE:
        return -fm.cap_top + fm.total_height / 2
    elif valign == TOP:
        return -fm.cap_top
    return -fm.bottom


def unified_alignment(entity: Union['Text', 'MText']) -> Tuple[int, int]:
    """ Return unified horizontal and vertical alignment.

    horizontal alignment: left=0, center=1, right=2

    vertical alignment: baseline=0, bottom=1, middle=2, top=3

    Returns:
        tuple(halign, valign)

    """
    dxftype = entity.dxftype()
    if dxftype in ('TEXT', 'ATTRIB', 'ATTDEF'):
        halign = entity.dxf.halign
        valign = entity.dxf.valign
        if halign in (3, 5):  # ALIGNED=3, FIT=5
            # For the alignments ALIGNED and FIT the text stretching has to be
            # handles separately.
            halign = CENTER
            valign = BASELINE
        elif halign == 4:  # MIDDLE is different to MIDDLE/CENTER
            halign = CENTER
            valign = X_MIDDLE
        return halign, valign
    elif dxftype == 'MTEXT':
        return MTEXT_ALIGN_FLAGS.get(entity.dxf.attachment_point, (LEFT, TOP))
    else:
        raise TypeError(f"invalid DXF {dxftype}")


def plain_text(text: str) -> str:
    chars = []
    raw_chars = list(reversed(validator.fix_one_line_text(caret_decode(text))))
    while len(raw_chars):
        char = raw_chars.pop()
        if char == '%':  # special characters
            if len(raw_chars) and raw_chars[-1] == '%':
                raw_chars.pop()  # discard next '%'
                if len(raw_chars):
                    special_char = raw_chars.pop()
                    # replace or discard formatting code
                    chars.append(SPECIAL_CHARS_ENCODING.get(special_char, ''))
            else:  # char is just a single '%'
                chars.append(char)
        else:  # char is what it is, a character
            chars.append(char)

    return "".join(chars)


ONE_CHAR_COMMANDS = "PNLlOoKkX"


##################################################
# MTEXT inline codes
# \L	Start underline
# \l	Stop underline
# \O	Start overstrike
# \o	Stop overstrike
# \K	Start strike-through
# \k	Stop strike-through
# \P	New paragraph (new line)
# \N	New column
# \pxi	Control codes for bullets, numbered paragraphs and columns
# \X	Paragraph wrap on the dimension line (only in dimensions)
# \Q	Slanting (oblique) text by angle - e.g. \Q30;
# \H	Text height - e.g. \H3x;
# \W	Text width - e.g. \W0.8x;
# \F	Font selection
#
#     e.g. \Fgdt;o - GDT-tolerance
#     e.g. \Fkroeger|b0|i0|c238|p10 - font Kroeger, non-bold, non-italic,
#     codepage 238, pitch 10
#
# \S	Stacking, fractions
#
#     e.g. \SA^B:
#     A
#     B
#     e.g. \SX/Y:
#     X
#     -
#     Y
#     e.g. \S1#4:
#     1/4
#
# \A	Alignment
#
#     \A0; = bottom
#     \A1; = center
#     \A2; = top
#
# \C	Color change
#
#     \C1; = red
#     \C2; = yellow
#     \C3; = green
#     \C4; = cyan
#     \C5; = blue
#     \C6; = magenta
#     \C7; = white
#
# \T	Tracking, char.spacing - e.g. \T2;
# \~	Non-wrapping space, hard space
# {}	Braces - define the text area influenced by the code
# \	Escape character - e.g. \\ = "\", \{ = "{"
#
# Codes and braces can be nested up to 8 levels deep

def plain_mtext(text: str, split=False) -> Union[List[str], str]:
    chars = []
    # split text into chars, in reversed order for efficient pop()
    raw_chars = list(reversed(text))
    while len(raw_chars):
        char = raw_chars.pop()
        if char == '\\':  # is a formatting command
            try:
                char = raw_chars.pop()
            except IndexError:
                break  # premature end of text - just ignore

            if char in '\\{}':
                chars.append(char)
            elif char in ONE_CHAR_COMMANDS:
                if char == 'P':  # new line
                    chars.append('\n')
                elif char == 'N':  # new column
                    # until columns are supported, better to at least remove the
                    # escape character
                    chars.append(' ')
                else:
                    pass  # discard other commands
            else:  # more character commands are terminated by ';'
                stacking = char == 'S'  # stacking command surrounds user data
                first_char = char
                search_chars = raw_chars.copy()
                try:
                    while char != ';':  # end of format marker
                        char = search_chars.pop()
                        if stacking and char != ';':
                            # append user data of stacking command
                            chars.append(char)
                    raw_chars = search_chars
                except IndexError:
                    # premature end of text - just ignore
                    chars.append('\\')
                    chars.append(first_char)
        elif char in '{}':  # grouping
            pass  # discard group markers
        elif char == '%':  # special characters
            if len(raw_chars) and raw_chars[-1] == '%':
                raw_chars.pop()  # discard next '%'
                if len(raw_chars):
                    special_char = raw_chars.pop()
                    # replace or discard formatting code
                    chars.append(SPECIAL_CHARS_ENCODING.get(special_char, ""))
            else:  # char is just a single '%'
                chars.append(char)
        else:  # char is what it is, a character
            chars.append(char)

    result = "".join(chars)
    return result.split('\n') if split else result


def caret_decode(text: str) -> str:
    """ DXF stores some special characters using caret notation. This function
    decodes this notation to normalise the representation of special characters
    in the string.

    see: <https://en.wikipedia.org/wiki/Caret_notation>

    """

    def replace_match(match: "re.Match") -> str:
        c = ord(match.group(1))
        return chr((c - 64) % 126)

    return re.sub(r'\^(.)', replace_match, text)


def split_mtext_string(s: str, size: int = 250) -> List[str]:
    chunks = []
    pos = 0
    while True:
        chunk = s[pos:pos + size]
        if len(chunk):
            if len(chunk) < size:
                chunks.append(chunk)
                return chunks
            pos += size
            # do not split chunks at '^'
            if chunk[-1] == '^':
                chunk = chunk[:-1]
                pos -= 1
            chunks.append(chunk)
        else:
            return chunks


def escape_dxf_line_endings(text: str) -> str:
    # replacing '\r\n' and '\n' by '\P' is required when exporting, else an
    # invalid DXF file would be created.
    return text.replace('\r', '').replace('\n', '\\P')


def replace_non_printable_characters(text: str, replacement: str = '▯') -> str:
    return ''.join(replacement if is_non_printable_char(c) else c for c in text)


def is_non_printable_char(char: str) -> bool:
    return 0 <= ord(char) < 32 and char != '\t'


def text_wrap(text: str, box_width: Optional[float],
              get_text_width: Callable[[str], float]) -> List[str]:
    """ Wrap text at "\n" and given `box_width`. This tool was developed for
    usage with the MTEXT entity. This isn't the most straightforward word
    wrapping algorithm, but it aims to match the behavior of AutoCAD.

    Args:
        text: text to wrap, included "\n" are handled as manual line breaks
        box_width: wrapping length, `None` to just wrap at "\n"
        get_text_width: callable which returns the width of the given string

    """
    # Copyright (c) 2020-2021, Matthew Broadway
    # License: MIT License
    if not text or text.isspace():
        return []
    manual_lines = re.split(r'(\n)', text)  # includes \n as it's own token
    tokens = [t for line in manual_lines for t in re.split(r'(\s+)', line) if t]
    lines = []
    current_line = ''
    line_just_wrapped = False

    for t in tokens:
        on_first_line = not lines
        if t == '\n' and line_just_wrapped:
            continue
        line_just_wrapped = False
        if t == '\n':
            lines.append(current_line.rstrip())
            current_line = ''
        elif t.isspace():
            if current_line or on_first_line:
                current_line += t
        else:
            if box_width is not None and get_text_width(
                    current_line + t) > box_width:
                if not current_line:
                    current_line += t
                else:
                    lines.append(current_line.rstrip())
                    current_line = t
                    line_just_wrapped = True
            else:
                current_line += t

    if current_line and not current_line.isspace():
        lines.append(current_line.rstrip())
    return lines


def is_text_vertical_stacked(text: 'DXFEntity') -> bool:
    """ Returns ``True`` if the associated text STYLE is vertical stacked.
    """
    if not text.is_supported_dxf_attrib('style'):
        raise TypeError(
            f'{text.dxftype()} does not support the style attribute.')

    if text.doc:
        style = text.doc.styles.get(text.dxf.style)
        if style:
            return style.is_vertical_stacked
    return False
