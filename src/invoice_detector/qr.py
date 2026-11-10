"""A small QR code generator (byte mode, error correction level M, versions 1-10), used to
show the two-factor set-up link to an authenticator app. Standard library only.

How a QR code is built:
1. The text becomes a bit stream: a 4-bit mode, the length, the bytes, then padding.
2. The stream is split into blocks, and each block gets Reed-Solomon error-correction
   bytes, so the code still reads when up to ~15% of it is damaged (level M).
3. Fixed patterns are drawn (three finder squares, timing lines, alignment squares),
   then the data bits are laid out in a zig-zag from the bottom-right corner.
4. One of eight masks is XORed over the data to avoid confusing patterns; the mask with
   the lowest penalty score wins, and its number is written into the format bits.
"""
import base64

# Level M block structure per version: (EC codewords per block, [data codewords per block])
BLOCKS_M = {
    1: (10, [16]), 2: (16, [28]), 3: (26, [44]), 4: (18, [32, 32]), 5: (24, [43, 43]),
    6: (16, [27] * 4), 7: (18, [31] * 4), 8: (22, [38, 38, 39, 39]), 9: (22, [36, 36, 36, 37, 37]),
    10: (26, [43, 43, 43, 43, 44]),
}
ALIGNMENT = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
             7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
EC_LEVEL_M = 0  # format bits for level M


# ---------------------------------------------------------------- Reed-Solomon over GF(256)
def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


# ---------------------------------------------------------------- encoding
def _version_for(length: int) -> int:
    for version, (_, blocks) in BLOCKS_M.items():
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * length <= 8 * sum(blocks):
            return version
    raise ValueError("Text too long for a version 1-10 QR code")


def _codewords(data: bytes, version: int) -> list[int]:
    ec_len, blocks = BLOCKS_M[version]
    capacity = sum(blocks)
    bits = [0, 1, 0, 0]  # byte mode
    count_bits = 8 if version < 10 else 16
    bits += [(len(data) >> i) & 1 for i in range(count_bits - 1, -1, -1)]
    for byte in data:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity * 8 - len(bits))      # terminator
    bits += [0] * (-len(bits) % 8)                        # to a byte boundary
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    # split into blocks, add error correction, interleave
    divisor = _rs_divisor(ec_len)
    chunks, pos = [], 0
    for size in blocks:
        chunks.append(words[pos:pos + size])
        pos += size
    ecc = [_rs_remainder(c, divisor) for c in chunks]
    out = []
    for i in range(max(blocks)):
        out += [c[i] for c in chunks if i < len(c)]
    for i in range(ec_len):
        out += [e[i] for e in ecc]
    return out


class _Matrix:
    def __init__(self, version: int):
        self.version = version
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set_fn(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.function[y][x] = True

    def draw_function_patterns(self) -> None:
        n = self.size
        for i in range(n):  # timing patterns
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):  # finder patterns + separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set_fn(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        positions = ALIGNMENT[self.version]
        for i, ay in enumerate(positions):
            for j, ax in enumerate(positions):
                last = len(positions) - 1
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue  # these would overlap the finder patterns
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)  # reserve the format areas; real bits drawn after masking
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self.set_fn(a, b, bit)
                self.set_fn(b, a, bit)

    def draw_format(self, mask: int) -> None:
        n = self.size
        data = EC_LEVEL_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1
        for i in range(6):
            self.set_fn(8, i, bit(i))
        self.set_fn(8, 7, bit(6))
        self.set_fn(8, 8, bit(7))
        self.set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fn(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fn(8, n - 15 + i, bit(i))
        self.set_fn(8, n - 8, True)  # the always-dark module

    def draw_codewords(self, words: list[int]) -> None:
        n = self.size
        i = 0
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5  # skip the vertical timing column
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.function[y][x] and i < len(words) * 8:
                        self.dark[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        conditions = [
            lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
            lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
            lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
        ]
        cond = conditions[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and cond(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        """Simplified ISO 18004 penalty: long runs, 2x2 blocks, finder-like runs, balance."""
        n, grid, score = self.size, self.dark, 0
        lines = [row for row in grid] + [[grid[y][x] for y in range(n)] for x in range(n)]
        finder_like = ([True, False, True, True, True, False, True, False, False, False, False],
                       [False, False, False, False, True, False, True, True, True, False, True])
        for line in lines:
            run, prev = 0, None
            for cell in line:
                if cell == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run, prev = 1, cell
            if run >= 5:
                score += 3 + run - 5
            for k in range(n - 10):
                if line[k:k + 11] in finder_like:
                    score += 40
        for y in range(n - 1):
            for x in range(n - 1):
                if grid[y][x] == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in grid)
        score += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
        return score


def matrix(text: str) -> list[list[bool]]:
    """QR code for text as rows of booleans (True = dark), without the quiet zone."""
    data = text.encode("utf-8")
    version = _version_for(len(data))
    words = _codewords(data, version)
    best, best_score = None, None
    for mask in range(8):
        m = _Matrix(version)
        m.draw_function_patterns()
        m.draw_codewords(words)
        m.apply_mask(mask)
        m.draw_format(mask)
        score = m.penalty()
        if best_score is None or score < best_score:
            best, best_score = m, score
    return best.dark


def svg(text: str, scale: int = 6, border: int = 4) -> str:
    grid = matrix(text)
    n = len(grid) + 2 * border
    path = "".join(f"M{x + border},{y + border}h1v1h-1z"
                   for y, row in enumerate(grid) for x, dark in enumerate(row) if dark)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * scale}" '
            f'height="{n * scale}" shape-rendering="crispEdges"><rect width="100%" height="100%" '
            f'fill="#fff"/><path d="{path}" fill="#000"/></svg>')


def svg_data_uri(text: str) -> str:
    """For <img src=...>: allowed by the web app's Content-Security-Policy (img-src data:)."""
    return "data:image/svg+xml;base64," + base64.b64encode(svg(text).encode()).decode()
