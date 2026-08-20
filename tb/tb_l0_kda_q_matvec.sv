`timescale 1ns/1ps
module tb_l0_kda_q_matvec;
  import golden_name_hash_pkg::*;
  localparam int MEM_BYTES=262144, DATA_W=24;
  logic clk=0,rst_n=0,start=0,busy,loaded,error;
  logic [1024*128-1:0] q_bus; logic [128*128-1:0] s_bus,z_bus;
  logic dv,dr,drv,drr,xv,xr,xrv,xrr,pending,is_dma; logic [35:0] da,xa; logic [127:0] dd,xd,pdata;
  logic [7:0] memory[0:MEM_BYTES-1]; logic signed [64*DATA_W-1:0] x_bus,y_bus;
  integer i,cycles; string image_path;
  qsz_tensor_loader #(.MAX_S_BEATS(128),.MAX_Z_BEATS(128)) load (
    .clk,.rst_n,.descriptor_base(36'd256),.descriptor_count(32'd246),.start,.busy,.loaded,.error,
    .q_name_hash(H_L0_KDA_Q_Q),.q_bytes(32'd2048),.s_name_hash(H_L0_KDA_Q_S),.s_bytes(32'd128),.z_name_hash(H_L0_KDA_Q_Z),.z_bytes(32'd64),.q_bus,.s_bus,.z_bus,
    .desc_mem_req_valid(dv),.desc_mem_req_ready(dr),.desc_mem_req_addr(da),.desc_mem_rsp_valid(drv),.desc_mem_rsp_ready(drr),.desc_mem_rsp_rdata(dd),.dma_mem_req_valid(xv),.dma_mem_req_ready(xr),.dma_mem_req_addr(xa),.dma_mem_rsp_valid(xrv),.dma_mem_rsp_ready(xrr),.dma_mem_rsp_rdata(xd));
  q4_dequant_matvec #(.IN(64),.OUT(64),.GROUP(64),.DATA_W(DATA_W)) mm (.x_bus,.q_bus(q_bus[2048*8-1:0]),.scale_bus(s_bus[64*16-1:0]),.zero_bus(z_bus[64*8-1:0]),.y_bus);
  always #5 clk=~clk; assign dr=!pending; assign xr=!pending; assign drv=pending&&!is_dma; assign xrv=pending&&is_dma; assign dd=pdata; assign xd=pdata;
  always_ff @(posedge clk) if(!rst_n) begin pending<=0; pdata<='0; is_dma<=0; end else begin
    if(pending&&((!is_dma&&drr)||(is_dma&&xrr))) pending<=0;
    if(!pending&&((dv&&dr)||(xv&&xr))) begin is_dma<=xv; for(i=0;i<16;i=i+1) pdata[i*8+:8]<=memory[(xv?xa:da)+i]; pending<=1; end
  end
  initial begin
    if(!$value$plusargs("IMAGE=%s",image_path)) $fatal(1,"IMAGE required");
    for(i=0;i<MEM_BYTES;i=i+1) memory[i]=0; $readmemh(image_path,memory); x_bus='0; for(i=0;i<64;i=i+1) x_bus[i*DATA_W+:DATA_W]=16384;
    repeat(4) @(posedge clk); rst_n=1; @(posedge clk); start=1; @(posedge clk); start=0;
    for(cycles=0;cycles<20000;cycles=cycles+1) begin @(posedge clk); if(error)$fatal(1,"load error"); if(loaded) begin $write("Y_Q14"); for(i=0;i<64;i=i+1)$write(" %0d",$signed(y_bus[i*DATA_W+:DATA_W])); $write("\n"); $finish; end end
    $fatal(1,"timeout");
  end
endmodule
