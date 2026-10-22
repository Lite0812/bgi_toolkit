/**
 * bm_decoder.c - 高效的 BM 格式解码器（C 实现）
 * 编译（MSVC）：cl /nologo /utf-8 /LD /O2 /MT /arch:AVX2 bm_decoder.c /Febm_decoder.dll
 */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#ifdef _OPENMP
#include <omp.h>
#endif

#ifdef _WIN32
    #define DLL_EXPORT __declspec(dllexport)
#else
    #define DLL_EXPORT
#endif

// ==================== 常量定义 ====================
// DCT 系数表（来自 Python 的 DCT_Table）
static const double DCT_TABLE[64] = {
    1.00000000, 1.38703990, 1.30656302, 1.17587554,
    1.00000000, 0.78569496, 0.54119611, 0.27589938,
    1.38703990, 1.92387950, 1.81225491, 1.63098633,
    1.38703990, 1.08979023, 0.75066054, 0.38268343,
    1.30656302, 1.81225491, 1.70710683, 1.53635550,
    1.30656302, 1.02655995, 0.70710677, 0.36047992,
    1.17587554, 1.63098633, 1.53635550, 1.38268340,
    1.17587554, 0.92387950, 0.63637930, 0.32442334,
    1.00000000, 1.38703990, 1.30656302, 1.17587554,
    1.00000000, 0.78569496, 0.54119611, 0.27589938,
    0.78569496, 1.08979023, 1.02655995, 0.92387950,
    0.78569496, 0.61731654, 0.42521504, 0.21677275,
    0.54119611, 0.75066054, 0.70710677, 0.63637930,
    0.54119611, 0.42521504, 0.29289323, 0.14931567,
    0.27589938, 0.38268343, 0.36047992, 0.32442334,
    0.27589938, 0.21677275, 0.14931567, 0.07612047
};

// Zigzag 顺序（从 Python 的 block_fill_order 得来）
static const uint8_t ZIGZAG[64] = {
     0,  1,  8, 16,  9,  2,  3, 10,
    17, 24, 32, 25, 18, 11,  4,  5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13,  6,  7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63
};

// ==================== 比特流操作 ====================
typedef struct {
    const uint8_t* data;
    int len;
    int pos;          // 当前读取字节索引
    int bit_buf;      // 当前字节的剩余位（从高位开始）
    int bit_count;    // 当前字节中剩余有效位数
} bitstream_t;

static void bs_init(bitstream_t* bs, const uint8_t* data, int len) {
    bs->data = data;
    bs->len = len;
    bs->pos = 0;
    bs->bit_buf = 0;
    bs->bit_count = 0;
}

// 读取 1 比特，返回 -1 表示错误
static int bs_get_bit(bitstream_t* bs) {
    if (bs->bit_count == 0) {
        if (bs->pos >= bs->len) return -1;
        bs->bit_buf = bs->data[bs->pos++];
        bs->bit_count = 8;
    }
    int bit = (bs->bit_buf >> 7) & 1;
    bs->bit_buf <<= 1;
    bs->bit_count--;
    return bit;
}

// 读取 count 比特，存入 *out，返回 0 成功，-1 失败
static int bs_get_bits(bitstream_t* bs, int count, uint32_t* out) {
    uint32_t val = 0;
    for (int i = 0; i < count; i++) {
        int bit = bs_get_bit(bs);
        if (bit < 0) return -1;
        val = (val << 1) | bit;
    }
    *out = val;
    return 0;
}

// 对齐到字节边界（跳过剩余位）
static void bs_align(bitstream_t* bs) {
    bs->bit_count = 0;
}

// 读取变长整数（小端序，每 7 位一组，最高位为 1 表示继续）
static int bs_read_varint(bitstream_t* bs, uint32_t* out) {
    uint32_t val = 0;
    int shift = 0;
    while (1) {
        if (bs->pos >= bs->len) return -1;
        uint8_t byte = bs->data[bs->pos++];
        val |= (byte & 0x7F) << shift;
        shift += 7;
        if (!(byte & 0x80)) break;
        if (shift > 32) return -1;
    }
    *out = val;
    return 0;
}

// 读取小端 32 位整数
static uint32_t read_le32(const uint8_t* p) {
    return p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24);
}

static uint16_t read_le16(const uint8_t* p) {
    return (uint16_t)(p[0] | (p[1] << 8));
}

// ==================== Huffman 树 ====================
typedef struct huff_node {
    uint32_t weight;
    int valid;
    int is_parent;
    int left, right;
} huff_node_t;

typedef struct {
    huff_node_t* nodes;
    int num_nodes;
    int root;
} huff_tree_t;

static huff_tree_t* build_huff_tree(const uint32_t* weights, int num_weights) {
    int max_nodes = num_weights * 2 + 1;
    huff_node_t* nodes = (huff_node_t*)calloc(max_nodes, sizeof(huff_node_t));
    if (!nodes) return NULL;

    uint64_t root_weight = 0;
    for (int i = 0; i < num_weights; i++) {
        nodes[i].weight = weights[i];
        nodes[i].valid = (weights[i] != 0);
        nodes[i].is_parent = 0;
        nodes[i].left = -1;
        nodes[i].right = -1;
        root_weight += weights[i];
    }

    int node_count = num_weights;
    while (1) {
        int child_indices[2] = { -1, -1 };
        uint32_t combined_weight = 0;

        for (int i = 0; i < 2; i++) {
            uint32_t min_weight = 0xFFFFFFFFu;
            int idx = -1;
            int n = 0;
            while (n < node_count) {
                if (nodes[n].valid) {
                    min_weight = nodes[n].weight;
                    idx = n;
                    n += 1;
                    break;
                }
                n += 1;
            }
            if (n < i + 1) n = i + 1;
            while (n < node_count) {
                if (nodes[n].valid && nodes[n].weight < min_weight) {
                    min_weight = nodes[n].weight;
                    idx = n;
                }
                n += 1;
            }
            child_indices[i] = idx;
            if (idx != -1) {
                nodes[idx].valid = 0;
                combined_weight += nodes[idx].weight;
            }
        }

        nodes[node_count].weight = combined_weight;
        nodes[node_count].valid = 1;
        nodes[node_count].is_parent = 1;
        nodes[node_count].left = child_indices[0];
        nodes[node_count].right = child_indices[1];
        node_count++;

        if ((uint64_t)combined_weight >= root_weight) break;
        if (node_count >= max_nodes) break;
    }

    huff_tree_t* tree = (huff_tree_t*)malloc(sizeof(huff_tree_t));
    if (!tree) { free(nodes); return NULL; }
    tree->nodes = nodes;
    tree->num_nodes = node_count;
    tree->root = node_count - 1;
    return tree;
}

static void free_huff_tree(huff_tree_t* tree) {
    if (tree) {
        free(tree->nodes);
        free(tree);
    }
}

// 解码一个符号，返回符号值，-1 表示错误
static int huff_decode(bitstream_t* bs, const huff_tree_t* tree) {
    if (!tree) return -1;
    int idx = tree->root;
    while (tree->nodes[idx].is_parent) {
        int bit = bs_get_bit(bs);
        if (bit < 0) return -1;
        if (bit == 0)
            idx = tree->nodes[idx].left;
        else
            idx = tree->nodes[idx].right;
        if (idx < 0) return -1;
    }
    return idx;
}

// ==================== DCT 和颜色转换 ====================
// 逆量化并执行 IDCT，输出到 out_float（64 个浮点数）
static void dequant_idct(const int32_t* quant_coeff, const double* dct_table, double* out_vals) {
    double tmp[8][8];
    double coeff[64];
    for (int i = 0; i < 64; i++) {
        coeff[i] = (double)quant_coeff[i] * dct_table[i];
    }

    // 水平 IDCT（按行）
    for (int i = 0; i < 8; i++) {
        double v1 = coeff[i];
        double v2 = coeff[8+i];
        double v3 = coeff[16+i];
        double v4 = coeff[24+i];
        double v5 = coeff[32+i];
        double v6 = coeff[40+i];
        double v7 = coeff[48+i];
        double v8 = coeff[56+i];

        double v10 = v1 + v5;
        double v11 = v1 - v5;
        double v12 = v3 + v7;
        double v13 = (v3 - v7) * 1.414213562 - v12;
        v1 = v10 + v12;
        v7 = v10 - v12;
        v3 = v11 + v13;
        v5 = v11 - v13;
        double v14 = v2 + v8;
        double v15 = v2 - v8;
        double v16 = v6 + v4;
        double v17 = v6 - v4;
        v8 = v14 + v16;
        double v11_ = (v14 - v16) * 1.414213562;
        double v9 = (v17 + v15) * 1.847759065;
        v10 = 1.082392200 * v15 - v9;
        v13 = -2.613125930 * v17 + v9;
        v6 = v13 - v8;
        v4 = v11_ - v6;
        v2 = v10 + v4;

        tmp[0][i] = v1 + v8;
        tmp[1][i] = v3 + v6;
        tmp[2][i] = v5 + v4;
        tmp[3][i] = v7 - v2;
        tmp[4][i] = v7 + v2;
        tmp[5][i] = v5 - v4;
        tmp[6][i] = v3 - v6;
        tmp[7][i] = v1 - v8;
    }

    // 垂直 IDCT（按列）
    for (int i = 0; i < 8; i++) {
        double v10 = tmp[i][0] + tmp[i][4];
        double v11 = tmp[i][0] - tmp[i][4];
        double v12 = tmp[i][2] + tmp[i][6];
        double v13_ = (tmp[i][2] - tmp[i][6]) * 1.414213562 - v12;
        double v1 = v10 + v12;
        double v7 = v10 - v12;
        double v3 = v11 + v13_;
        double v5 = v11 - v13_;
        double v14 = tmp[i][1] + tmp[i][7];
        double v15 = tmp[i][1] - tmp[i][7];
        double v16 = tmp[i][5] + tmp[i][3];
        double v17 = tmp[i][5] - tmp[i][3];
        double v8 = v14 + v16;
        double v11_ = (v14 - v16) * 1.414213562;
        double v9 = (v17 + v15) * 1.847759065;
        double v10_ = v9 - v15 * 1.082392200;
        double v13 = v9 - v17 * 2.613125930;
        double v6 = v13 - v8;
        double v4 = v11_ - v6;
        double v2 = v10_ - v4;

        out_vals[i*8 + 0] = v1 + v8;
        out_vals[i*8 + 1] = v3 + v6;
        out_vals[i*8 + 2] = v5 + v4;
        out_vals[i*8 + 3] = v7 + v2;
        out_vals[i*8 + 4] = v7 - v2;
        out_vals[i*8 + 5] = v5 - v4;
        out_vals[i*8 + 6] = v3 - v6;
        out_vals[i*8 + 7] = v1 - v8;
    }
}

// 将浮点值转换为 uint8_t（钳位到 0~255）
static inline uint8_t float_to_byte(double x) {
    if (x >= 255.0) return 255;
    if (x <= 0.0) return 0;
    return (uint8_t)x;
}

static inline uint8_t f2s(double f) {
    int a = 128 + ((int)f >> 3);
    if (a <= 0) return 0;
    if (a <= 255) return (uint8_t)a;
    if (a < 384) return 255;
    return 0;
}

// YCbCr 转 RGB（使用与 Python 相同的公式）
static void ycbcr_to_rgb(double y, double cb, double cr, uint8_t* rgba, int write_alpha) {
    double r = y + 1.402 * cr - 178.956;
    double g = y - 0.34414 * cb - 0.71414 * cr + 135.95984;
    double b = y + 1.772 * cb - 226.316;
    rgba[0] = float_to_byte(b);
    rgba[1] = float_to_byte(g);
    rgba[2] = float_to_byte(r);
    if (write_alpha) rgba[3] = 255;
}

// ==================== 块解码 ====================
static int decode_dct_block(const int32_t* data, int data_len, int src, const double* dct_table, double* out_block) {
    if (src < 0 || src + 63 >= data_len) return 0;
    dequant_idct(data + src, dct_table, out_block);
    return 1;
}

// ==================== Alpha 解码 ====================
// 参考 Python 的 unpack_alpha，采用 RLE 方式
static int decode_alpha(bitstream_t* bs, uint8_t* output, int width, int height) {
    // 读取 4 字节头部
    if (bs->pos + 4 > bs->len) return -1;
    uint32_t header = read_le32(bs->data + bs->pos);
    bs->pos += 4;
    if (header != 1) return -1; // 仅支持类型 1

    int dst = 3; // 输出缓冲区索引（Alpha 通道在 RGBA 的偏移 3）
    int ctl = 1 << 1; // 控制位掩码初始值（Python 中的 1<<1）
    int out_len = width * height * 4;

    while (dst < out_len) {
        ctl >>= 1;
        if (ctl == 1) {
            if (bs->pos >= bs->len) return -1;
            ctl = bs->data[bs->pos++] | 0x100;
        }

        if (ctl & 1) {
            // 复制模式
            if (bs->pos + 2 > bs->len) return -1;
            uint16_t v = read_le16(bs->data + bs->pos);
            bs->pos += 2;
            int x = v & 0x3F;
            if (x > 0x1F) x |= -0x40;
            int y = (v >> 6) & 7;
            if (y != 0) y |= -8;
            int count = ((v >> 9) & 0x7F) + 3;

            int src = dst + (x + y * width) * 4;
            if (src < 0 || src >= dst) return -1; // 无效偏移
            for (int i = 0; i < count; i++) {
                if (dst >= out_len || src >= out_len) return -1;
                output[dst] = output[src];
                src += 4;
                dst += 4;
            }
        } else {
            // 直接写入
            if (bs->pos >= bs->len) return -1;
            output[dst] = bs->data[bs->pos++];
            dst += 4;
        }
    }
    return 0;
}

static void swap_rb_inplace(uint8_t* buf, size_t size) {
    if (!buf || size < 4) return;
    for (size_t i = 0; i + 2 < size; i += 4) {
        uint8_t t = buf[i];
        buf[i] = buf[i + 2];
        buf[i + 2] = t;
    }
}

// ==================== 主函数 ====================
DLL_EXPORT int decode_bm_frame(const uint8_t* frame_data, int data_len,
                                int width, int height, int bpp,
                                const uint8_t* quant_tables,
                                uint8_t* output) {
    if (!frame_data || data_len <= 0 || !output) return -1;

    bitstream_t bs;
    bs_init(&bs, frame_data, data_len);

    // 1. 读取权重表
    uint32_t weights1[16];
    for (int i = 0; i < 16; i++) {
        if (bs_read_varint(&bs, &weights1[i]) < 0) return -1;
    }
    uint32_t weights2[176];
    for (int i = 0; i < 176; i++) {
        if (bs_read_varint(&bs, &weights2[i]) < 0) return -1;
    }

    // 2. 构建 Huffman 树
    huff_tree_t* tree1 = build_huff_tree(weights1, 16);
    huff_tree_t* tree2 = build_huff_tree(weights2, 176);
    if (!tree1 || !tree2) {
        free_huff_tree(tree1);
        free_huff_tree(tree2);
        return -2;
    }

    int width_padded = (width + 7) & ~7;
    int height_padded = (height + 7) & ~7;

    double dct_tables[2][64];
    for (int i = 0; i < 128; i++) {
        dct_tables[i >> 6][i & 63] = (double)quant_tables[i] * DCT_TABLE[i & 63];
    }
    int y_blocks = height_padded / 8;
    int offset_count = y_blocks + 1;

    int offset_table_pos = bs.pos;
    uint32_t* raw_offsets = (uint32_t*)malloc(offset_count * sizeof(uint32_t));
    if (!raw_offsets) {
        free_huff_tree(tree1);
        free_huff_tree(tree2);
        return -3;
    }
    for (int i = 0; i < offset_count; i++) {
        if (bs.pos + 4 > data_len) {
            free(raw_offsets);
            free_huff_tree(tree1);
            free_huff_tree(tree2);
            return -4;
        }
        raw_offsets[i] = read_le32(frame_data + bs.pos);
        bs.pos += 4;
    }

    const uint8_t* comp_data = frame_data + bs.pos;
    int comp_len = data_len - bs.pos;
    int input_base = offset_table_pos + offset_count * 4;

    int* offsets = (int*)malloc(offset_count * sizeof(int));
    if (!offsets) {
        free(raw_offsets);
        free_huff_tree(tree1);
        free_huff_tree(tree2);
        return -5;
    }
    for (int i = 0; i < offset_count; i++) {
        offsets[i] = (int)raw_offsets[i] - input_base;
    }

    int offsets_valid = 1;
    if (offset_count == 0) offsets_valid = 0;
    for (int i = 0; i < offset_count; i++) {
        if (offsets[i] < 0) { offsets_valid = 0; break; }
        if (i + 1 < offset_count && offsets[i] > offsets[i + 1]) { offsets_valid = 0; break; }
    }
    if (offsets_valid && offsets[offset_count - 1] > comp_len) offsets_valid = 0;

    uint8_t* alt_data = NULL;
    int alt_len = 0;
    if (!offsets_valid && offset_count > 0) {
        alt_len = comp_len + 4;
        alt_data = (uint8_t*)malloc(alt_len);
        if (alt_data) {
            uint32_t last_off = raw_offsets[offset_count - 1];
            alt_data[0] = (uint8_t)(last_off & 0xFF);
            alt_data[1] = (uint8_t)((last_off >> 8) & 0xFF);
            alt_data[2] = (uint8_t)((last_off >> 16) & 0xFF);
            alt_data[3] = (uint8_t)((last_off >> 24) & 0xFF);
            memcpy(alt_data + 4, comp_data, comp_len);

            int alt_count = y_blocks;
            int* alt_offsets = (int*)malloc(alt_count * sizeof(int));
            if (alt_offsets) {
                int alt_base = offset_table_pos + alt_count * 4;
                for (int i = 0; i < alt_count; i++) {
                    alt_offsets[i] = (int)raw_offsets[i] - alt_base;
                }
                int alt_valid = 1;
                if (alt_count == 0) alt_valid = 0;
                for (int i = 0; i < alt_count; i++) {
                    if (alt_offsets[i] < 0) { alt_valid = 0; break; }
                    if (i + 1 < alt_count && alt_offsets[i] > alt_offsets[i + 1]) { alt_valid = 0; break; }
                }
                if (alt_valid && alt_offsets[alt_count - 1] > alt_len) alt_valid = 0;
                if (alt_valid) {
                    free(offsets);
                    offsets = alt_offsets;
                    offset_count = alt_count;
                    comp_data = alt_data;
                    comp_len = alt_len;
                } else {
                    free(alt_offsets);
                    free(alt_data);
                    alt_data = NULL;
                }
            } else {
                free(alt_data);
                alt_data = NULL;
            }
        }
    }

    uint8_t* temp_output = (uint8_t*)calloc((size_t)width_padded * (size_t)height_padded * 4, 1);
    if (!temp_output) {
        free(raw_offsets);
        free(offsets);
        free_huff_tree(tree1);
        free_huff_tree(tree2);
        if (alt_data) free(alt_data);
        return -6;
    }
    for (int y = 0; y < height; y++) {
        memcpy(temp_output + (size_t)y * width_padded * 4, output + (size_t)y * width * 4, (size_t)width * 4);
    }

    int write_alpha = 1;
    int alpha_header = 0;
    if (bpp == 32 && offset_count > y_blocks) {
        int alpha_offset = offsets[y_blocks];
        if (alpha_offset >= 0 && alpha_offset + 4 <= comp_len) {
            alpha_header = (int)read_le32(comp_data + alpha_offset);
            if (alpha_header == 2) write_alpha = 0;
        }
    }

    int pad_skip = ((width_padded >> 3) + 7) >> 3;
    size_t row_stride = (size_t)width_padded * 4;
    int block_count = width_padded / 8;
    int max_block_size = block_count * 64 * 3;
#ifdef _OPENMP
#pragma omp parallel for schedule(static)
#endif
    for (int row = 0; row < y_blocks; row++) {
        if (row >= offset_count) continue;
        int row_off = offsets[row];
        // A start-only table has no terminal offset for the last strip.
        // An explicit next offset still bounds color data before Alpha.
        int next_off = row + 1 < offset_count ? offsets[row + 1] : comp_len;
        if (row_off < 0 || row_off >= comp_len) continue;
        if (next_off < 0 || next_off > comp_len) continue;
        int block_off = row_off + pad_skip;
        int block_len = next_off - block_off;
        if (block_len <= 0 || block_off < 0 || block_off >= comp_len) continue;

        const uint8_t* mask = NULL;
        if (row_off >= 0 && row_off + pad_skip <= comp_len) {
            mask = comp_data + row_off;
        }

        bitstream_t block_bs;
        bs_init(&block_bs, comp_data + block_off, block_len);

        uint32_t block_size_u = 0;
        if (bs_read_varint(&block_bs, &block_size_u) < 0) continue;
        int block_size = (int)block_size_u;
        if (block_size <= 0) continue;

        int32_t* color_data = NULL;
        if (max_block_size > 0) {
            color_data = (int32_t*)malloc((size_t)max_block_size * sizeof(int32_t));
        }
        if (block_size > max_block_size || !color_data) {
            if (color_data) free(color_data);
            continue;
        }
        memset(color_data, 0, (size_t)block_size * sizeof(int32_t));

        int32_t acc = 0;
        for (int i = 0; i < block_size; i += 64) {
            if (block_bs.pos >= block_len) break;
            int count = huff_decode(&block_bs, tree1);
            if (count < 0) break;
            if (count != 0) {
                uint32_t val = 0;
                if (bs_get_bits(&block_bs, count, &val) < 0) break;
                if ((val >> (count - 1)) == 0) {
                    val = ((uint32_t)(-1) << count) | val;
                    val += 1;
                }
                acc += (int32_t)val;
            }
            color_data[i] = acc;
        }

        if (block_bs.bit_count & 7) bs_align(&block_bs);

        for (int i = 0; i < block_size; i += 64) {
            if (block_bs.pos >= block_len) break;
            int index = 1;
            while (index < 64 && block_bs.pos < block_len) {
                int code = huff_decode(&block_bs, tree2);
                if (code < 0) { index = 64; break; }
                if (code == 0) break;
                if (code == 0xF) {
                    index += 0x10;
                    continue;
                }
                index += code & 0xF;
                if (index >= 64) break;
                int bits = code >> 4;
                uint32_t val = 0;
                if (bits > 0) {
                    if (bs_get_bits(&block_bs, bits, &val) < 0) { index = 64; break; }
                    if ((val >> (bits - 1)) == 0) {
                        val = ((uint32_t)(-1) << bits) | val;
                        val += 1;
                    }
                }
                int pos = i + ZIGZAG[index];
                if (pos >= 0 && pos < block_size) color_data[pos] = (int32_t)val;
                index += 1;
            }
        }

        if (!mask) {
            int plane_size = block_count * 64;
            for (int col = 0; col < block_count; col++) {
                int src_y = col * 64;
                int src_cb = src_y + plane_size;
                int src_cr = src_y + plane_size * 2;
                double block_y[64], block_cb[64], block_cr[64];
                if (!decode_dct_block(color_data, block_size, src_y, dct_tables[0], block_y)) continue;
                if (!decode_dct_block(color_data, block_size, src_cb, dct_tables[1], block_cb)) continue;
                if (!decode_dct_block(color_data, block_size, src_cr, dct_tables[1], block_cr)) continue;

                int base_x = col * 8;
                int base_y = row * 8;
                for (int py = 0; py < 8; py++) {
                    int yy = base_y + py;
                    if (yy >= height_padded) continue;
                    for (int px = 0; px < 8; px++) {
                        int xx = base_x + px;
                        if (xx >= width_padded) continue;
                        int idx = py * 8 + px;
                        uint8_t* dst = temp_output + ((size_t)yy * width_padded + xx) * 4;
                        double y_val = (double)f2s(block_y[idx]);
                        double cb_val = (double)f2s(block_cb[idx]);
                        double cr_val = (double)f2s(block_cr[idx]);
                        ycbcr_to_rgb(y_val, cb_val, cr_val, dst, write_alpha);
                    }
                }
            }
        } else {
            int present_count = 0;
            for (int col = 0; col < block_count; col++) {
                present_count += (mask[col >> 3] >> (col & 7)) & 1;
            }
            if (present_count > 0) {
                int plane_size = present_count * 64;
                int present_index = 0;
                for (int col = 0; col < block_count; col++) {
                    int bit = (mask[col >> 3] >> (col & 7)) & 1;
                    if (!bit) continue;
                    int src_base = present_index * 64;
                    int src_y = src_base;
                    int src_cb = src_base + plane_size;
                    int src_cr = src_base + plane_size * 2;
                    double block_y[64], block_cb[64], block_cr[64];
                    if (!decode_dct_block(color_data, block_size, src_y, dct_tables[0], block_y)) {
                        present_index++;
                        continue;
                    }
                    if (!decode_dct_block(color_data, block_size, src_cb, dct_tables[1], block_cb)) {
                        present_index++;
                        continue;
                    }
                    if (!decode_dct_block(color_data, block_size, src_cr, dct_tables[1], block_cr)) {
                        present_index++;
                        continue;
                    }

                    int base_x = col * 8;
                    int base_y = row * 8;
                    for (int py = 0; py < 8; py++) {
                        int yy = base_y + py;
                        if (yy >= height_padded) continue;
                        uint8_t* row_ptr = temp_output + (size_t)yy * row_stride;
                        for (int px = 0; px < 8; px++) {
                            int xx = base_x + px;
                            if (xx >= width_padded) continue;
                            int idx = py * 8 + px;
                            uint8_t* dst = row_ptr + (size_t)xx * 4;
                            double y_val = (double)f2s(block_y[idx]);
                            double cb_val = (double)f2s(block_cb[idx]);
                            double cr_val = (double)f2s(block_cr[idx]);
                            ycbcr_to_rgb(y_val, cb_val, cr_val, dst, write_alpha);
                        }
                    }
                    present_index++;
                }
            }
        }
        if (color_data) free(color_data);
    }

    if (bpp == 32 && offset_count > y_blocks) {
        int alpha_offset = offsets[y_blocks];
        if (alpha_header == 1 && alpha_offset >= 0 && alpha_offset < comp_len) {
            bitstream_t alpha_bs;
            bs_init(&alpha_bs, comp_data + alpha_offset, comp_len - alpha_offset);
            decode_alpha(&alpha_bs, temp_output, width_padded, height_padded);
        }
    }

    for (int y = 0; y < height; y++) {
        memcpy(output + (size_t)y * width * 4, temp_output + (size_t)y * width_padded * 4, (size_t)width * 4);
    }

    free(temp_output);
    free(raw_offsets);
    free(offsets);
    free_huff_tree(tree1);
    free_huff_tree(tree2);
    if (alt_data) free(alt_data);
    return 0;
}

DLL_EXPORT void bm_decoder_set_threads(int n) {
#ifdef _OPENMP
    if (n > 0) {
        omp_set_num_threads(n);
    } else {
        omp_set_num_threads(omp_get_max_threads());
    }
#endif
}

DLL_EXPORT int decode_bm_frame_rgba(const uint8_t* frame_data, int data_len,
                                int width, int height, int bpp,
                                const uint8_t* quant_tables,
                                uint8_t* output) {
    if (!output) return -1;
    size_t out_size = (size_t)width * (size_t)height * 4;
    swap_rb_inplace(output, out_size);
    int ret = decode_bm_frame(frame_data, data_len, width, height, bpp, quant_tables, output);
    if (ret != 0) return ret;
    swap_rb_inplace(output, out_size);
    return 0;
}
