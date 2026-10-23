// Independent CBG V2 / BF_Movie frame writer.
// Format verified against export-for-ai: 14010AA90, 1401077F0,
// 1401043C0, 140104050, 140107160 and 1401034E0.
// Reuses this project's MIT-licensed GARbro-derived tree/decoder primitives.
struct V2Bits {
    Bytes data; unsigned cache=0, count=0;
    void put(uint32_t v, unsigned n) {
        while(n){--n;cache=(cache<<1)|((v>>n)&1);if(++count==8){data.push_back(uint8_t(cache));cache=count=0;}}
    }
    void align(){if(count){data.push_back(uint8_t(cache<<(8-count)));cache=count=0;}}
};
struct V2Codes {
    std::vector<std::vector<uint8_t>> codes;
    explicit V2Codes(const std::vector<uint32_t>& weights):codes(weights.size()) {
        Tree tree(weights,true);
        std::function<void(int,std::vector<uint8_t>)> visit=[&](int n,std::vector<uint8_t> path){
            if(n<0)return;
            if(tree.nodes[n].first<0){codes[n]=std::move(path);return;}
            auto left=path;left.push_back(0);visit(tree.nodes[n].first,std::move(left));
            path.push_back(1);visit(tree.nodes[n].second,std::move(path));
        };visit(tree.root,{});
    }
    void put(V2Bits& bits,unsigned symbol)const{for(auto bit:codes.at(symbol))bits.put(bit,1);}
};
static unsigned v2_width(int value){unsigned n=0,v=unsigned(value<0?-value:value);while(v){v>>=1;++n;}return n;}
static void v2_number(V2Bits& bits,int value,unsigned n){if(n)bits.put(value>=0?unsigned(value):unsigned(value+(1<<n)-1),n);}
// JPEG's orthonormal DCT. The decoder's AAN factors and final /8 give
// the same coefficient normalization; tables remain in natural order.
static const std::array<std::array<double,8>,8>& v2_basis(){
    static const auto basis=[](){std::array<std::array<double,8>,8> b{};
        for(int u=0;u<8;u++)for(int x=0;x<8;x++)b[u][x]=(u?0.5:std::sqrt(0.125))*std::cos((2*x+1)*u*3.14159265358979323846/16);
        return b;}();return basis;
}
static int v2_sample(double v){return std::max(0,std::min(255,int(v)));}
static void v2_fdct(const double* samples,const uint8_t* quant,int32_t* output){
    const auto& b=v2_basis();double tmp[64];
    for(int y=0;y<8;y++)for(int u=0;u<8;u++){double s=0;for(int x=0;x<8;x++)s+=(samples[y*8+x]-128)*b[u][x];tmp[y*8+u]=s;}
    for(int v=0;v<8;v++)for(int u=0;u<8;u++){double s=0;for(int y=0;y<8;y++)s+=tmp[y*8+u]*b[v][y];output[v*8+u]=int32_t(std::round(s/quant[v*8+u]));}
}
template<class Sink> static void v2_ac(const int32_t* block,Sink sink){
    unsigned zeros=0;
    for(unsigned j=1;j<64;j++){
        int value=block[ZIGZAG[j]];
        if(!value){++zeros;continue;}
        while(zeros>=16){sink(15,0,0);zeros-=16;}
        unsigned n=v2_width(value);require(n<=10,"DCT AC category overflow");
        sink((n<<4)|zeros,value,n);zeros=0;
    }
    if(zeros)sink(0,0,0);
}
static Bytes v2_alpha(const Bytes& alpha,unsigned width){
    Bytes output(4,0);put32(output,0,1);size_t position=0;
    while(position<alpha.size()){
        size_t control=output.size();output.push_back(0);
        for(unsigned bit=0;bit<8 && position<alpha.size();bit++){
            size_t best=0,best_distance=0;int best_y=0;
            // Runs and previous scanlines cover flat masks efficiently.
            for(unsigned row=0;row<=7;row++){
                size_t distance=row?size_t(row)*width:1;
                if(distance>position)continue;
                size_t count=0,limit=std::min(size_t(130),alpha.size()-position);
                while(count<limit && alpha[position+count]==alpha[position+count-distance])++count;
                if(count>best){best=count;best_distance=distance;best_y=-int(row);if(best==limit)break;}
            }
            if(best>=3){
                unsigned x=best_y?0:63;
                unsigned token=x|((unsigned(best_y)&7)<<6)|(unsigned(best-3)<<9);
                output[control]|=uint8_t(1u<<bit);output.push_back(uint8_t(token));output.push_back(uint8_t(token>>8));position+=best;
            }else output.push_back(alpha[position++]);
        }
    }
    return output;
}
static Bytes v2_frame(const Bytes& raw,unsigned width,unsigned height,unsigned bpp,const Bytes& quant){
    require(width&&height&&width<=65535&&height<=65535&&size_t(width)*height<=134217728,"invalid V2 dimensions");
    require((bpp==8||bpp==24||bpp==32)&&raw.size()==size_t(width)*height*(bpp/8),"invalid V2 pixel buffer");
    require(quant.size()==128 && std::all_of(quant.begin(),quant.end(),[](uint8_t q){return q!=0;}),"invalid V2 quantization tables");
    unsigned pw=(width+7)&~7,ph=(height+7)&~7,rows=ph/8,cols=pw/8,channels=bpp==8?1:3;
    size_t per_row=size_t(cols)*64*channels;
    std::vector<std::vector<int32_t>> coefficients(rows);
    std::vector<uint32_t> dc_weights(16),ac_weights(176);
    for(unsigned row=0;row<rows;row++){
        auto& values=coefficients[row];values.resize(per_row);
        for(unsigned channel=0;channel<channels;channel++)for(unsigned col=0;col<cols;col++){
            double samples[64];
            for(unsigned j=0;j<64;j++){
                unsigned x=col*8+j%8,y=row*8+j/8;double value=0;
                if(x<width && y<height){
                    size_t p=(size_t(y)*width+x)*(bpp/8);
                    if(channels==1)value=raw[p];
                    else {double blue=raw[p],green=raw[p+1],red=raw[p+2];
                        if(!channel)value=v2_sample(0.299*red+0.587*green+0.114*blue+0.5);
                        else if(channel==1)value=v2_sample(-0.16874*red-0.33126*green+0.5*blue+128.5);
                        else value=v2_sample(0.5*red-0.41869*green-0.08131*blue+128.5);}
                } else if(channel)value=128;
                samples[j]=value;
            }
            v2_fdct(samples,quant.data()+(channel?64:0),values.data()+(size_t(channel)*cols+col)*64);
        }
        int previous=0;
        for(size_t i=0;i<per_row;i+=64){int delta=values[i]-previous;previous=values[i];unsigned n=v2_width(delta);require(n<16,"DCT DC category overflow");++dc_weights[n];
            v2_ac(values.data()+i,[&](unsigned symbol,int,unsigned){++ac_weights[symbol];});}
    }
    V2Codes dc(dc_weights),ac(ac_weights);Bytes output;
    for(auto weight:dc_weights)putv(output,weight);for(auto weight:ac_weights)putv(output,weight);
    size_t offsets=output.size();unsigned offset_count=rows+(bpp==32?1:0);
    output.resize(offsets+size_t(offset_count)*4);
    for(unsigned row=0;row<rows;row++){
        require(output.size()<=0xffffffffu,"V2 frame too large");put32(output,offsets+row*4,uint32_t(output.size()));
        unsigned mask_length=(cols+7)/8;
        output.insert(output.end(),mask_length,255); // all blocks are present (key frame)
        putv(output,uint32_t(per_row));V2Bits bits;const auto& values=coefficients[row];int previous=0;
        for(size_t i=0;i<per_row;i+=64){int delta=values[i]-previous;previous=values[i];unsigned n=v2_width(delta);dc.put(bits,n);v2_number(bits,delta,n);}
        bits.align();
        for(size_t i=0;i<per_row;i+=64)v2_ac(values.data()+i,[&](unsigned symbol,int value,unsigned n){ac.put(bits,symbol);v2_number(bits,value,n);});
        bits.align();output.insert(output.end(),bits.data.begin(),bits.data.end());
    }
    if(bpp==32){
        put32(output,offsets+rows*4,uint32_t(output.size()));Bytes alpha(size_t(pw)*ph,0);
        for(unsigned y=0;y<height;y++)for(unsigned x=0;x<width;x++)alpha[size_t(y)*pw+x]=raw[(size_t(y)*width+x)*4+3];
        Bytes packed=v2_alpha(alpha,pw);output.insert(output.end(),packed.begin(),packed.end());
    }
    return output;
}
static Bytes cbg2_encode(const Bytes& raw,unsigned width,unsigned height,unsigned bpp,const Bytes& supplied){
    require(supplied.size()==176,"V2 requires named quantization tables in supplied header");
    Bytes quant(supplied.begin()+48,supplied.end());Bytes body=v2_frame(raw,width,height,bpp,quant);
    Bytes header(supplied.begin(),supplied.begin()+48);memcpy(header.data(),"CompressedBG___\0",16);
    header[16]=uint8_t(width);header[17]=uint8_t(width>>8);header[18]=uint8_t(height);header[19]=uint8_t(height>>8);
    put32(header,20,bpp);put32(header,32,0);put32(header,40,132);header[44]=header[45]=0;header[46]=2;header[47]=0;
    quant.resize(132);put32(quant,128,180);
    for(auto c:quant){header[44]+=c;header[45]^=c;}
    quant=cipher(quant,u32(header,36),true);header.insert(header.end(),quant.begin(),quant.end());
    header.insert(header.end(),body.begin(),body.end());return header;
}
