#!/usr/bin/python2

from __future__ import division, print_function

import sys
import argparse
import pickle
import re
from abc import ABCMeta, abstractmethod

import Image

ap = argparse.ArgumentParser()
ap.add_argument('bg_bitmap', help="The single image or the 1-bit mask")
ap.add_argument('fg_bitmap', nargs='?',
                help="If given, the 1-bit foreground pixels")

ap.add_argument('--x-hit', '-x', type=int, default=None,
                help="x-coordinate of the cursor hotspot")
ap.add_argument('--y-hit', '-y', type=int, default=None,
                help="y-coordinate of the cursor hotspot")

ap.add_argument('--fg-color', '-f', default=None,
                help="The foreground colour (necessary only if the colours "
                "cannot be guessed from the image file). Accepted formats:"
                "colour name, rgb(255, 50, 0), rgb(1.0, 0.2, 0.0), "
                "#ff7700, #f70")
ap.add_argument('--bg-color', '-b', default=None,
                help="The background colour.")

ap.add_argument('--output', '-o', type=argparse.FileType('wb'),
                default=sys.stdout,
                help="The output file, by default stdout")
ap.add_argument('--debug', action='store_true', default=False,
                help="Check for consistency and print"
                "the bitmaps to stdout")

class Bitmap(object):
    def __init__(self, width, height, buf=None):
        self.width = width
        self.height = height
        self.pitch = ((width + 7) // 8)

        if buf is not None:
            if len(buf) != self.height * self.pitch:
                raise ValueError
            self.buffer = buf
        else:
            self.wipe()

    def __str__(self):
        lines = []
        for i in range(self.height):
            lines.append(''.join(
                    'o' if bit else '.'
                    for byte in self.buffer[i*self.pitch:(i+1)*self.pitch]
                    for bit in ((byte >> j) & 0x1 for j in range(8))
                    )[:self.width])
        return '\n'.join(lines)

    def wipe(self):
        self.buffer = bytearray(b'\0' * (self.pitch * self.height))

    def __getitem__(self, pos):
        i, j = pos
        if i >= self.width or j >= self.height:
            raise IndexError
        h_byte = j * self.pitch
        w_byte, bit = divmod(i, 8)
        return (self.buffer[h_byte + w_byte] >> bit) & 0x1

    def __setitem__(self, pos, value):
        i, j = pos
        if i >= self.width or j >= self.height:
            raise IndexError
        h_byte = j * self.pitch
        w_byte, bit = divmod(i, 8)
        if value:
            self.buffer[h_byte + w_byte] |= 0x1 << bit
        else:
            self.buffer[h_byte + w_byte] &= ~(0x1 << bit)

    def __hash__(self):
        raise TypeError

    def __eq__(self, other):
        return isinstance(other, Bitmap) and self.width == other.width \
            and self.height == other.height and self.buffer == other.buffer

    def __invert__(self):
        return bytearray(~i for i in self.buffer)

    def _copy(self):
        return self.__class__(self.width, self.height, self.buffer)

    def __iand__(self, other):
        if not isinstance(other, Bitmap):
            raise TypeError

        for i, v in enumerate(other.buffer):
            self.buffer[i] &= v

        return self

    def __ior__(self, other):
        if not isinstance(other, Bitmap):
            raise TypeError

        for i, v in enumerate(other.buffer):
            self.buffer[i] |= v

        return self

    def __ixor__(self, other):
        if not isinstance(other, Bitmap):
            raise TypeError

        for i, v in enumerate(other.buffer):
            self.buffer[i] ^= v

        return self

    def __and__(self, other):
        cpy = self._copy()
        cpy &= other
        return cpy

    def __or__(self, other):
        cpy = self._copy()
        cpy |= other
        return cpy

    def __xor__(self, other):
        cpy = self._copy()
        cpy |= other
        return cpy

class ColorHandlerMeta(ABCMeta):

    def __new__(cls, name, bases, dict):
        res = super(ColorHandlerMeta, cls).__new__(cls, name, bases, dict)
        res._register_recurse(res, set())
        return res

    def _register_recurse(cls, sub_class, marked):
        marked.add(cls)
        for base in cls.__bases__:
            if isinstance(base, ColorHandlerMeta) and base not in marked:
                base._register_subclass(sub_class, marked)

    def _register_subclass(cls, sub_class, marked):
        if hasattr(cls, 'MODES'):
            for mode in sub_class.MODE:
                cls.MODES[mode] = sub_class
        else:
            cls._register_recurse(sub_class, marked)

class ColorHandler(object):
    __metaclass__ = ColorHandlerMeta
    MODES = {}

    def __new__(cls, PIL_image, **kwargs):
        return super(ColorHandler, cls).__new__(cls.MODES[PIL_image.mode], PIL_image)

    def __init__(self, PIL_image, thresh=127):
        self._image = PIL_image
        self._threshold = thresh

    # RATIONALE: why factories of filters instead of a filter method?
    # Because the filter may be run on all the pixels of the image
    # therfore being potentially a bottleneck, a short lambda can be
    # faster than the entire method
    @abstractmethod
    def make_transparency_filter(self):
        pass


class RGBColorHandler(ColorHandler):
    MODE = ['RGB']

    def make_transparency_filter(self):
        if 'transparency' in self._image.info:
            transparent_color = self._image.info['transparency']
            return lambda x: x == transparent_color
        else:
            return lambda x: False


class RGBAColorHandler(ColorHandler):
    MODE = ['RGBA', 'RGBa']

    def make_transparency_filter(self):
        threshold = self._threshold
        return lambda x: x[3] < threshold


class LColorHandler(ColorHandler):
    MODE = ['L']

    def make_transparency_filter(self):
        if 'transparency' in self._image.info:
            transparent_color = self._image.info['transparency']
            return lambda x: x == transparent_color
        else:
            return lambda x: False

class PColorHander(ColorHandler):
    MODE = ['P']

    def make_transparency_filter(self):
        if 'transparency' in self._image.info:
            transparent_color = self._image.info['transparency']
            return lambda x: transparent_color == x
        else:
            return lambda x: False

class OneColorHandler(ColorHandler):
    MODE = ['1']

    def make_transparency_filter(self):
        return lambda x: False


class FixedPalette(object):
    """Read-access wrapper around ImagingPalettes as the latter is
    entirely borken"""

    def __init__(self, palette):
        self._palette = bytearray(palette.palette)

    def __getitem__(self, item):
        return tuple(self._palette[i] for i in range(3*item, 3*item+3))


class LockMaker(object):
    RGB_TRIPLE_RE = \
        r'\s*rgb\s*\(\s*([0-9\.]+)\s*,\s*([0-9\.]+)\s*,\s*([0-9\.]+)\s*\)\s*'

    def __init__(self, args):
        self.args = args
        self.color_mode = None
        self.width = None
        self.height = None

        self.stroke_border = False
        self._fg_filter = None
        self._bg_filter = None

        self._bg_bitmap_raw = Image.open(args.bg_bitmap, "r")
        self._fg_bitmap_raw = None
        self.uni_image = False
        if args.fg_bitmap is not None:
            self._fg_bitmap_raw = Image.open(args.fg_bitmap, "r")
        else:
            self.uni_image = True


        self._guess_size()
        self._guess_hotspot()
        self._guess_colors()

        self._fg_bitmap = Bitmap(self.width, self.height)
        self._bg_bitmap = Bitmap(self.width, self.height)

        if self.uni_image:
            self._stroke(self._bg_bitmap_raw, self._bg_bitmap, self._bg_filter)

            if self.stroke_border:
                self._stroke_border()
            else:
                self._stroke(self._bg_bitmap_raw, self._fg_bitmap,
                             self._fg_filter)
        else:
            self._stroke(self._fg_bitmap_raw, self._fg_bitmap, self._fg_filter)
            self._stroke(self._bg_bitmap_raw, self._bg_bitmap, self._bg_filter)
            self._bg_bitmap |= self._fg_bitmap

        if self.args.debug:
            print(str(self._bg_bitmap))
            print(str(self._fg_bitmap))

            assert self._bg_bitmap & self._fg_bitmap == self._fg_bitmap
            assert self._bg_bitmap | self._fg_bitmap == self._bg_bitmap

    def _guess_size(self):
        bg_width, bg_height = self._bg_bitmap_raw.size
        fg_width, fg_height = self._bg_bitmap_raw.size

        if not bg_height == fg_height and not bg_width == fg_width:
            print("The sizes of the images do not match", file=sys.stderr)
            sys.exit(1)

        self.height = bg_height
        self.width = bg_width

    def _guess_hotspot(self):
        if args.x_hit is not None:
            self.x_hot = args.x_hot
        elif 'hotspot' in self._bg_bitmap_raw.info:
            self.x_hot = self._bg_bitmap_raw.info['hotspot'][0]
        elif not self.uni_image and 'hotspot' in self._fg_bitmap_raw.info:
            self.x_hot = self._fg_bitmap_raw.info['hotspot'][0]
        else:
            self.x_hot = self.width // 2 + 1

        if args.y_hit is not None:
            self.y_hot = args.y_hot
        elif 'hotspot' in self._bg_bitmap_raw.info:
            self.y_hot = self._bg_bitmap_raw.info['hotspot'][1]
        elif not self.uni_image and 'hotspot' in self._fg_bitmap_raw.info:
            self.y_hot = self._fg_bitmap_raw.info['hotspot'][1]
        else:
            self.y_hot = self.height // 2 + 1

    def _guess_colors(self):
        image_has_colors = False
        bg_hist = self._histogram(self._bg_bitmap_raw)
        if not self.uni_image:
            fg_hist = self._histogram(self._fg_bitmap_raw)

        if self.uni_image:
            mode = self._bg_bitmap_raw.mode
            info = self._bg_bitmap_raw.info

            bg_color_handler = ColorHandler(self._bg_bitmap_raw)
            tr_filter = bg_color_handler.make_transparency_filter()
            effective_colors = {}
            for color, num in bg_hist.items():
                if not tr_filter(color):
                    effective_colors[color] = num

            n_effective_colors = len(effective_colors)

            if mode in ('RGB', 'RGBA', 'RGBa', 'P'):
                if n_effective_colors == 1:
                    self.stroke_border = True
                    self._bg_filter = lambda x: not tr_filter(x)
                elif n_effective_colors == 2:
                    image_has_colors = True
                    f, b = effective_colors
                    if mode == 'RGB':
                        image_fg, image_bg = f, b
                    elif mode == 'RGBA' or mode == 'RGBa':
                        image_fg, image_bg = f[:3], b[:3]
                    elif mode == 'P':
                        plte = FixedPalette(self._bg_bitmap_raw.palette)
                        image_fg, image_bg = plte[f], plte[b]
                    else:
                        raise Exception("Can't happen")
                    self._bg_filter = lambda x: x == f or x == b
                    self._fg_filter = lambda x: x == f
                else:
                    print("Too many colors in image", file=sys.stderr)
                    sys.exit(1)

            elif mode == 'L':
                if n_effective_colors == 1:
                    self.stroke_border = True
                    self._bg_filter = lambda x: not tr_filter(x)
                elif n_effective_colors == 2:
                    image_has_colors = True
                    f, b = effective_colors
                    image_fg = (f, f, f)
                    image_bg = (b, b, b)
                    self.fg_filter = lambda x: f
                    self._bg_filter = lambda x: not tr_filter(x)

            elif mode == '1':
                self._bg_filter = lambda x: bool(x)
                self.stroke_border = True
            else:
                print("Unsopported image mode", file=sys.stderr)
                sys.exit(1)
        else:
            mode = self._bg_bitmap_raw.mode
            info = self._bg_bitmap_raw.info

            mode_fg = self._fg_bitmap_raw.mode
            if mode_fg != mode:
                print("Mode mismatch. Only 1-bit bitmaps supported"
                      " for dual-image mode", file=sys.stderr)
                sys.exit(1)

            if mode in ('RGB', 'RGBA', 'RGBa', 'P', 'L'):
                print("Unsupported image mode for dual-image"
                      " (obviously pointless)", file=sys.stderr)
                sys.exit(1)
            elif mode == '1':
                self._fg_filter = lambda x: bool(x)
                self._bg_filter = lambda x: bool(x)
            else:
                print("Unsopported image mode", file=sys.stderr)
                sys.exit(1)

        if self.args.fg_color is not None:
            if self.args.bg_color is None:
                print("Inconsistent color specification", file=sys.stderr)
                sys.exit(1)
            self.fg_color = self._parse_color(self.args.fg_color)
            self.bg_color = self._parse_color(self.args.bg_color)
        elif image_has_colors:
            self._check_color_mode('rgb')
            self.fg_color = image_fg
            self.bg_color = image_bg
        else:
            if self.uni_image:
                self.stroke_border = True
            self.fg_color = 'white'
            self.bg_color = 'black'
            self.color_mode = 'named'

    def _check_color_mode(self, color_mode):
        if self.color_mode is None:
            self.color_mode = color_mode
        elif self.color_mode != color_mode:
            print("Color mode mismatch", file=sys.stderr)
            sys.exit(1)

    def _histogram(self, PIL_img):
        hist = {}
        data = PIL_img.load()
        width, height = PIL_img.size
        for i in range(width):
            for j in range(height):
                pixel = data[i, j]
                if pixel not in hist:
                    hist[pixel] = 0
                hist[pixel] += 1
        return hist

    def _stroke(self, PIL_img, bitmap, filter, wipe=True):
        if wipe:
            bitmap.wipe()
        data = PIL_img.load()
        for i in range(self.width):
            for j in range(self.height):
                if filter(data[i, j]):
                    bitmap[i,j] = 1

    def _stroke_border(self):
        def action(i, j, di, dj, in_img):
            if self._bg_bitmap[i,j]:
                if not in_img:
                    self._fg_bitmap[i,j] = 1
                return True
            else:
                if in_img:
                    self._fg_bitmap[i-di, j-dj] = 1
                return False

        def finish(i, j, in_img):
            if in_img:
                self._fg_bitmap[i, j] = 1


        # stroke vertically
        for i in range(self.width):
            in_img = False
            for j in range(self.height):
                in_img = action(i, j, 0, 1, in_img)
            finish(i, j,  in_img)

        # stroke horizontally
        for j in range(self.height):
            in_img = False
            for i in range(self.width):
                in_img = action(i, j, 1, 0, in_img)
            finish(i, j, in_img)

    def _parse_color(self, color_string):
        """Parse a string representing a color the formats
        * rgb(255, 127, 0)
        * rgb(1.0, 0.5, 0.0)
        * #f70
        * #ff7f00
        * named color
        """
        if color_string.startswith('#'):
            self._check_color_mode('rgb')

            if len(color_string) == 4:
                return tuple(17*int(color_string[i], base=16)
                             for i in range(1,4))
            elif len(color_string) == 7:
                return tuple(int(color_string[i:i+1], base=16)
                             for i in range(1,6,2))
            else:
                print("Invalid color format", file=sys.stderr)
                sys.exit(1)
        else:
            match = re.match(self.RGB_TRIPLE_RE, color_string)
            if match is not None:
                self._check_color_mode('rgb')

                try:
                    r, g, b = map(int, (match.group(i) for i in range(1, 4)))
                except ValueError:
                    try:
                        r, g, b = map(lambda x: int(float(x)*255),
                                      (match.group(i) for i in range(1, 4)))
                    except ValueError:
                        print("Invalid color format", file=sys.stderr)
                        sys.exit(1)

                # note: no check for negative values is required as
                # the regex does not allow -
                if r > 255 or g > 255 or b > 255:
                    print("Invalid color format", file=sys.stderr)
                    sys.exit(1)
                return (r, g, b)

        self._check_color_mode('named')
        return color_string

    @property
    def fg_bitmap(self):
        return bytes(self._fg_bitmap.buffer)

    @property
    def bg_bitmap(self):
        return bytes(self._bg_bitmap.buffer)


args = ap.parse_args()

lock_maker = LockMaker(args)


with args.output as f:
    pickle.dump({
            "width": lock_maker.width,
            "height": lock_maker.height,
            "x_hot": lock_maker.x_hot,
            "y_hot": lock_maker.y_hot,
            "fg_bitmap": lock_maker.fg_bitmap,
            "bg_bitmap": lock_maker.bg_bitmap,
            "color_mode": lock_maker.color_mode,
            "bg_color": lock_maker.bg_color,
            "fg_color": lock_maker.fg_color
    }, f, protocol=2)
