"""Turns an uploaded profile picture into the small square JPEG that is stored.

The server does not trust the client to have scaled the picture: whatever is uploaded is decoded here (with limits),
cropped to the centre, scaled to AVATAR_SIDE pixels, and written again as a JPEG, so the stored picture is always small
and carries no metadata (the place and time of a photo, for one)."""
import io

from PIL import Image, ImageOps, UnidentifiedImageError

AVATAR_SIDE = 256
JPEG_QUALITY = 88
# A picture bigger than this many pixels is refused before it is decoded (a small file can unpack to a huge image).
MAX_SOURCE_PIXELS = 40_000_000
_ACCEPTED_FORMATS = ["PNG", "JPEG", "WEBP"]


class AvatarImageError(Exception):
    """The upload is not a picture the server can use. `too_large` tells a picture of too many pixels from one that cannot be read."""

    def __init__(self, message: str, *, too_large: bool = False) -> None:
        super().__init__(message)
        self.too_large = too_large


def make_avatar_jpeg(data: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(data), formats=_ACCEPTED_FORMATS) as image:
            width, height = image.size
            if width * height > MAX_SOURCE_PIXELS:
                raise AvatarImageError("The picture has too many pixels", too_large=True)
            image.load()
            # The first frame of an animated picture; the camera's rotation applied; transparency over white.
            picture = ImageOps.exif_transpose(image)
            if picture.mode in ("RGBA", "LA") or "transparency" in picture.info:
                rgba = picture.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                picture = flat
            else:
                picture = picture.convert("RGB")
            picture = ImageOps.fit(picture, (AVATAR_SIDE, AVATAR_SIDE), method=Image.Resampling.LANCZOS)
            out = io.BytesIO()
            picture.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return out.getvalue()
    except AvatarImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise AvatarImageError("The picture cannot be read") from exc
