// BGI codecs. Format algorithms adapted from GARbro (MIT, morkt 2014-2018)
// and EthornellTools (MIT, arcusmaximus). See THIRD_PARTY_NOTICES.md.
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <queue>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>
#include "bm_decoder.c"

using Bytes = std::vector<uint8_t>;
static void require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
static uint32_t u32(const Bytes& b, size_t p) { require(p + 4 <= b.size(), "truncated integer"); return read_le32(b.data() + p); }
static void put32(Bytes& b, size_t p, uint32_t v) { for (int i=0;i<4;i++) b[p+i]=uint8_t(v>>(8*i)); }
static uint32_t vint(const Bytes& b, size_t& p) {
    uint32_t v=0;
    for(int s=0;s<35;s+=7) { require(p<b.size(),"truncated varint"); unsigned c=b[p++]; v|=(c&127)<<s; if(c<128)return v; }
    throw std::runtime_error("varint overflow");
}
static void putv(Bytes& b, uint32_t v) { while(v>=128){b.push_back(uint8_t(v|128));v>>=7;} b.push_back(uint8_t(v)); }
static Bytes cipher(Bytes data, uint32_t seed, bool encode=false, uint32_t magic=0) {
    for(auto& c:data) {
        uint32_t a=20021*(seed&65535), k=((magic|(seed>>16))*20021+seed*346+(a>>16))&65535;
        seed=(k<<16)+(a&65535)+1; c=uint8_t(encode?c+uint8_t(k):c-uint8_t(k));
    }
    return data;
}
struct Tree {
    std::vector<std::pair<int,int>> nodes;
    int root;
    explicit Tree(const std::vector<uint32_t>& weights, bool v2=false) {
        if(v2) {
            huff_tree_t* source=build_huff_tree(weights.data(),int(weights.size()));
            require(source!=nullptr,"cannot build v2 Huffman tree");
            nodes.resize(source->num_nodes,{-1,-1});
            for(int i=0;i<source->num_nodes;i++) if(source->nodes[i].is_parent)
                nodes[i]={source->nodes[i].left,source->nodes[i].right};
            root=source->root;free_huff_tree(source);return;
        }
        using Entry=std::pair<uint64_t,int>;
        std::priority_queue<Entry,std::vector<Entry>,std::greater<Entry>> heap;
        nodes.resize(weights.size(),{-1,-1});
        for(size_t i=0;i<weights.size();i++)if(weights[i])heap.push({weights[i],int(i)});
        require(!heap.empty(),"empty Huffman tree");
        while(heap.size()>1){auto a=heap.top();heap.pop();auto b=heap.top();heap.pop();int n=int(nodes.size());nodes.push_back({a.second,b.second});heap.push({a.first+b.first,n});}
        root=heap.top().second;
        if(nodes[root].first<0){nodes.push_back({root,-1});root=int(nodes.size())-1;}
    }
    int token(bitstream_t& bs) const {
        int n=root;
        while(nodes[n].first>=0){int bit=bs_get_bit(&bs);require(bit>=0,"truncated Huffman stream");n=bit?nodes[n].second:nodes[n].first;require(n>=0,"invalid Huffman branch");}
        return n;
    }
};
static Bytes cbg_decode(const Bytes& input) {
    require(input.size()>=48 && !memcmp(input.data(),"CompressedBG___",14),"invalid CBG header");
    unsigned w=read_le16(input.data()+16),h=read_le16(input.data()+18),bpp=u32(input,20),version=read_le16(input.data()+46);
    require(w&&h&&size_t(w)*h<=134217728,"invalid image dimensions");
    require(bpp==8||bpp==16||bpp==24||bpp==32,"unsupported pixel depth");
    size_t enc_len=u32(input,40);require(enc_len<=input.size()-48,"truncated encoded table");
    Bytes table=cipher(Bytes(input.begin()+48,input.begin()+48+enc_len),u32(input,36));
    uint8_t sum=0,xorv=0;for(auto c:table){sum+=c;xorv^=c;}
    require(sum==input[44]&&xorv==input[45],"CBG table checksum mismatch");
    if(version<2) {
        std::vector<uint32_t> weights(256);size_t p=0;for(auto& v:weights)v=vint(table,p);
        size_t intermediate=u32(input,32),bytes=size_t(w)*h*(bpp/8);
        require(intermediate<=bytes*6+1024,"invalid intermediate size");
        Tree tree(weights);Bytes packed(intermediate);bitstream_t bits;bs_init(&bits,input.data()+48+enc_len,int(input.size()-48-enc_len));
        for(auto& v:packed)v=uint8_t(tree.token(bits));
        Bytes raw(bytes,0);p=0;size_t dst=0;bool zero=false;
        while(dst<bytes){size_t count=vint(packed,p);require(count<=bytes-dst,"RLE output overflow");if(!zero){require(count<=packed.size()-p,"truncated RLE data");memcpy(raw.data()+dst,packed.data()+p,count);p+=count;}dst+=count;zero=!zero;}
        unsigned ch=bpp/8;size_t stride=size_t(w)*ch;
        for(unsigned y=0;y<h;y++)for(unsigned x=0;x<w;x++)for(unsigned c=0;c<ch;c++){
            size_t i=size_t(y)*stride+x*ch+c;unsigned avg=0;if(x)avg+=raw[i-ch];if(y)avg+=raw[i-stride];if(x&&y)avg/=2;raw[i]=uint8_t(raw[i]+avg);
        }
        return raw;
    }
    require(version==2 && table.size()>=128 && bpp!=16,"unsupported CBG version");
    Bytes stream(input.begin()+48+enc_len,input.end());size_t pos=0;
    std::vector<uint32_t> w1(16),w2(176);for(auto& v:w1)v=vint(stream,pos);for(auto& v:w2)v=vint(stream,pos);
    Tree t1(w1,true),t2(w2,true);unsigned pw=(w+7)&~7,ph=(h+7)&~7,rows=ph/8,cols=pw/8;
    require(pos+size_t(rows+1)*4<=stream.size(),"truncated DCT offsets");
    std::vector<uint32_t> offsets(rows+1);for(auto& v:offsets){v=u32(stream,pos);pos+=4;}
    Bytes rgba(size_t(pw)*ph*4,0);double quant[2][64];for(unsigned i=0;i<128;i++)quant[i/64][i%64]=table[i]*DCT_TABLE[i%64];
    unsigned channels=bpp==8?1:3,mask_len=(cols+7)/8;
    for(unsigned row=0;row<rows;row++){
        size_t start=size_t(offsets[row])+mask_len,end=row+1<rows?offsets[row+1]:stream.size();
        if(row+1==rows && bpp==32 && offsets[rows]>=start && offsets[rows]<=stream.size())end=offsets[rows];
        require(start<end&&end<=stream.size(),"invalid DCT strip range");
        size_t count=vint(stream,start);require(count==size_t(cols)*64*channels,"invalid DCT coefficient count");
        std::vector<int32_t> values(count,0);bitstream_t bits;bs_init(&bits,stream.data()+start,int(end-start));
        int acc=0;for(size_t i=0;i<count;i+=64){int n=t1.token(bits);require(n<32,"invalid DC width");uint32_t v=0;if(n){require(!bs_get_bits(&bits,n,&v),"truncated DC");acc+=v<(1u<<(n-1))?int(v)-int((1u<<n)-1):int(v);}values[i]=acc;}
        bs_align(&bits);
        for(size_t i=0;i<count;i+=64)for(int j=1;j<64;){int code=t2.token(bits);if(!code)break;if(code==15){j+=16;continue;}j+=code&15;if(j>=64)break;int n=code>>4;uint32_t v=0;require(!bs_get_bits(&bits,n,&v),"truncated AC");values[i+ZIGZAG[j]]=n&&v<(1u<<(n-1))?int(v)-int((1u<<n)-1):int(v);j++;}
        for(unsigned col=0;col<cols;col++){
            double values_y[64],values_cb[64],values_cr[64];dequant_idct(values.data()+col*64,quant[0],values_y);
            if(channels==3){dequant_idct(values.data()+(cols+col)*64,quant[1],values_cb);dequant_idct(values.data()+(2*cols+col)*64,quant[1],values_cr);}
            for(unsigned j=0;j<64;j++){uint8_t* dst=rgba.data()+((size_t(row*8+j/8)*pw)+col*8+j%8)*4;
                if(channels==1){dst[0]=dst[1]=dst[2]=f2s(values_y[j]);dst[3]=255;}
                else ycbcr_to_rgb(f2s(values_y[j]),f2s(values_cb[j]),f2s(values_cr[j]),dst,1);
            }
        }
    }
    if(bpp==32 && offsets[rows]+4<=stream.size() && u32(stream,offsets[rows])==1){bitstream_t bs;bs_init(&bs,stream.data()+offsets[rows],int(stream.size()-offsets[rows]));require(!decode_alpha(&bs,rgba.data(),pw,ph),"invalid alpha stream");}
    Bytes output(size_t(w)*h*(bpp/8));
    for(unsigned y=0;y<h;y++)for(unsigned x=0;x<w;x++){
        const uint8_t* src=rgba.data()+(size_t(y)*pw+x)*4;size_t dst=(size_t(y)*w+x)*(bpp/8);
        memcpy(output.data()+dst,src,bpp/8);
    }
    return output;
}
static Bytes cbg_encode(const Bytes& raw, unsigned width,unsigned height,unsigned bpp,const Bytes& supplied) {
    require(width&&height&&width<=65535&&height<=65535,"invalid CBG dimensions");unsigned ch=bpp/8;
    require((bpp==8||bpp==24||bpp==32)&&raw.size()==size_t(width)*height*ch,"invalid pixel buffer");
    Bytes delta=raw;size_t stride=size_t(width)*ch;
    for(unsigned y=0;y<height;y++)for(unsigned x=0;x<width;x++)for(unsigned c=0;c<ch;c++){
        size_t i=size_t(y)*stride+x*ch+c;unsigned avg=0;if(x)avg+=raw[i-ch];if(y)avg+=raw[i-stride];if(x&&y)avg/=2;delta[i]=uint8_t(raw[i]-avg);
    }
    Bytes packed;size_t literal=0,i=0;
    while(i<delta.size()){
        if(delta[i]){i++;continue;}size_t start=i;while(i<delta.size()&&!delta[i])i++;
        if(i-start>4){putv(packed,uint32_t(start-literal));packed.insert(packed.end(),delta.begin()+literal,delta.begin()+start);putv(packed,uint32_t(i-start));literal=i;}
    }
    if(literal<delta.size()){putv(packed,uint32_t(delta.size()-literal));packed.insert(packed.end(),delta.begin()+literal,delta.end());}
    std::vector<uint32_t> weights(256);for(auto c:packed)weights[c]++;Tree tree(weights);
    std::array<std::vector<uint8_t>,256> codes;
    std::function<void(int,std::vector<uint8_t>)> visit=[&](int n,std::vector<uint8_t> path){if(n<0)return;if(tree.nodes[n].first<0){codes[n]=path;return;}auto left=path;left.push_back(0);visit(tree.nodes[n].first,left);path.push_back(1);visit(tree.nodes[n].second,path);};visit(tree.root,{});
    Bytes table,body;for(auto v:weights)putv(table,v);uint8_t cache=0;int bits=0;
    for(auto c:packed)for(auto bit:codes[c]){cache=uint8_t((cache<<1)|bit);if(++bits==8){body.push_back(cache);bits=0;cache=0;}}
    if(bits)body.push_back(uint8_t(cache<<(8-bits)));
    Bytes header=supplied.size()==48?supplied:Bytes(48,0);memcpy(header.data(),"CompressedBG___\0",16);
    header[16]=uint8_t(width);header[17]=uint8_t(width>>8);header[18]=uint8_t(height);header[19]=uint8_t(height>>8);
    put32(header,20,bpp);put32(header,32,uint32_t(packed.size()));put32(header,40,uint32_t(table.size()));header[44]=header[45]=0;
    for(auto c:table){header[44]+=c;header[45]^=c;}header[46]=1;header[47]=0;
    table=cipher(table,u32(header,36),true);header.insert(header.end(),table.begin(),table.end());header.insert(header.end(),body.begin(),body.end());return header;
}
#include "cbg_v2_encode.h"

static Bytes dsc_decode(const Bytes& data) {
    require(data.size()>=544&&!memcmp(data.data(),"DSC FORMAT 1.00\0",16),"invalid DSC file");
    size_t size=u32(data,20),tokens=u32(data,24);require(size<=536870912,"DSC output too large");
    Bytes depths=cipher(Bytes(data.begin()+32,data.begin()+544),u32(data,16),false,uint32_t(read_le16(data.data()))<<16);
    std::vector<std::pair<unsigned,int>> symbols;for(int i=0;i<512;i++)if(depths[i])symbols.push_back({depths[i],i});std::sort(symbols.begin(),symbols.end());
    struct Node{int child[2]={-1,-1};int symbol=-1;};std::vector<Node> tree(1);uint64_t code=0;unsigned previous=0;
    for(auto item:symbols){unsigned depth=item.first;require(depth<=32,"invalid DSC depth");code<<=depth-previous;int node=0;for(int j=int(depth)-1;j>=0;j--){unsigned bit=unsigned((code>>j)&1);if(tree[node].child[bit]<0){int next=int(tree.size());tree[node].child[bit]=next;tree.emplace_back();}node=tree[node].child[bit];}tree[node].symbol=item.second;code++;previous=depth;}
    Bytes output;output.reserve(size);bitstream_t bs;bs_init(&bs,data.data()+544,int(data.size()-544));
    for(size_t i=0;i<tokens;i++){int node=0;while(tree[node].symbol<0){int bit=bs_get_bit(&bs);require(bit>=0,"truncated DSC stream");node=tree[node].child[bit];require(node>=0,"invalid DSC code");}int symbol=tree[node].symbol;
        if(symbol<256)output.push_back(uint8_t(symbol));else{uint32_t distance=0;require(!bs_get_bits(&bs,12,&distance),"truncated DSC distance");distance+=2;require(distance<=output.size(),"DSC reference out of range");for(int j=0;j<(symbol&255)+2;j++)output.push_back(output[output.size()-distance]);}require(output.size()<=size,"DSC output overflow");}
    require(output.size()==size,"DSC size mismatch");return output;
}

extern "C" __declspec(dllexport) void bgi_resource_free(void* ptr) { free(ptr); }
extern "C" __declspec(dllexport) int bgi_resource_transform(int operation,const uint8_t* input,size_t size,
    unsigned width,unsigned height,unsigned bpp,const uint8_t* header,size_t header_size,
    uint8_t** result,size_t* result_size,char* error,size_t error_size) {
    try {
        require(input&&result&&result_size,"invalid arguments");Bytes data(input,input+size),output;
        if(operation==0)output=cbg_decode(data);
        else if(operation==1)output=cbg_encode(data,width,height,bpp,header?Bytes(header,header+header_size):Bytes());
        else if(operation==2)output=dsc_decode(data);
        else if(operation==3)output=cbg2_encode(data,width,height,bpp,header?Bytes(header,header+header_size):Bytes());
        else throw std::runtime_error("unknown operation");
        *result=static_cast<uint8_t*>(malloc(output.size()?output.size():1));require(*result!=nullptr,"out of memory");
        memcpy(*result,output.data(),output.size());*result_size=output.size();return 0;
    } catch(const std::exception& ex) { if(error&&error_size){strncpy_s(error,error_size,ex.what(),_TRUNCATE);}return -1; }
}
