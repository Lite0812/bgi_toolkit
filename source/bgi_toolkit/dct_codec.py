"""CBG V2 / BF_Movie quantization settings, in natural coefficient order."""
from .i18n import tr
from .resource_codec import ResourceError
from .resource_metadata import byte_values

LUMINANCE = [16,11,10,16,24,40,51,61,12,12,14,19,26,58,60,55,
    14,13,16,24,40,57,69,56,14,17,22,29,51,87,80,62,
    18,22,37,56,68,109,103,77,24,35,55,64,81,104,113,92,
    49,64,78,87,103,121,120,101,72,92,95,98,112,100,103,99]
CHROMINANCE = [17,18,24,47,99,99,99,99,18,21,26,66,99,99,99,99,
    24,26,56,99,99,99,99,99,47,66,99,99,99,99,99,99] + [99]*32


def quantization_tables(metadata=None, quality=None):
    if quality is None and metadata:
        result = byte_values(metadata.get('luminance',[]), tr('亮度量化表')) + byte_values(metadata.get('chrominance',[]), tr('色度量化表'))
        if len(result) != 128 or 0 in result:
            raise ResourceError(tr('量化表应各有 64 个 1～255 的整数'))
        return result
    quality = 90 if quality is None else quality
    if type(quality) is not int or not 1 <= quality <= 100:
        raise ResourceError(tr('DCT 质量必须在 1～100 之间'))
    # Recovered from sub_1401091D0; low quality blends towards 255.
    return bytes(max(1,min(255,int(255-(255-n)*quality/50 if quality < 50 else n*(100-quality)/50)))
        for n in LUMINANCE+CHROMINANCE)
