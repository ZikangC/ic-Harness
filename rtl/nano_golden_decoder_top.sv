// Standard-image golden decoder integration.  It deliberately shares the
// header and token reader on arbiter master 0: header traffic has completed
// before token reads begin, so four masters remain sufficient.
module nano_golden_decoder_top #(
  parameter int DATA_W = 24, parameter int MAX_SEQ = 64
) (
  input logic clk, input logic rst_n, input logic run,
  output logic busy, output logic done, output logic error,
  output logic mem_req_valid, input logic mem_req_ready, output logic mem_req_write,
  output logic [35:0] mem_req_addr, output logic [127:0] mem_req_wdata,
  output logic [15:0] mem_req_wstrb, input logic mem_rsp_valid,
  output logic mem_rsp_ready, input logic [127:0] mem_rsp_rdata
);
  typedef enum logic [2:0] {IDLE,HDR_START,HDR_WAIT,EXEC_START,EXEC_WAIT,DONE,ERROR} top_t;
  typedef enum logic [1:0] {TR_IDLE,TR_REQ,TR_WAIT,TR_DONE} token_t;
  top_t top_state; token_t token_state;
  logic header_start, header_busy, header_done, header_error;
  logic [31:0] n_layers,d_model,vocab,max_seq_cfg,seq_len,group_size,bitmap,desc_count,logit_stride;
  logic [35:0] desc_addr,tokens_addr,logits_addr,argmax_addr,status_addr;
  logic ctrl_busy,ctrl_done,ctrl_error,token_read_valid,token_read_done,begin_token,dp_busy,dp_done,dp_error;
  logic [31:0] token_index,write_token_index; logic write_row_valid,write_final_status,write_row_done;
  logic [8:0] token_id; logic [31:0] token_word;
  logic signed [512*DATA_W-1:0] logits_q; logic [8:0] argmax_q;
  logic signed [512*32-1:0] logits_i32; integer i;
  logic writer_busy,writer_row_done,writer_run_done;

  logic hqv,hqr,hrv,hrr,tqv,tqr,trv,trr,dqv,dqr,drv,drr,xqv,xqr,xrv,xrr,wqv,wqr,wqwr;
  logic [35:0] hqa,tqa,dqa,xqa,wqa; logic [127:0] hrd,trd,drd,xrd,wqd,wrd; logic [15:0] wqs;
  logic a0qv,a0qr,a0rv,a0rr; logic [127:0] a0rd;

  image_header_reader header (.clk,.rst_n,.start(header_start),.busy(header_busy),.done(header_done),.error(header_error),
    .mem_req_valid(hqv),.mem_req_ready(hqr),.mem_req_addr(hqa),.mem_rsp_valid(hrv),.mem_rsp_ready(hrr),.mem_rsp_rdata(hrd),
    .n_layers,.d_model,.vocab,.max_seq(max_seq_cfg),.seq_len,.group_size,.layer_type_bitmap(bitmap),.descriptor_count(desc_count),
    .descriptor_addr(desc_addr),.tokens_addr,.logits_addr,.argmax_addr,.status_addr,.logit_stride);
  nano_golden_microop_controller ctrl (.clk,.rst_n,.start_run(top_state==EXEC_START),.seq_len,.busy(ctrl_busy),.run_done(ctrl_done),
    .token_read_valid,.token_index,.token_read_done,.begin_token,.datapath_busy(dp_busy),.datapath_token_done(dp_done),.datapath_error(dp_error),
    .write_row_valid,.write_token_index,.write_final_status,.write_row_done,.error(ctrl_error));
  nano_golden_datapath_fsm #(.DATA_W(DATA_W),.MAX_SEQ(MAX_SEQ)) dp (
    .clk,.rst_n,.begin_token,.token_id,.token_position(token_index[5:0]),.descriptor_base(desc_addr),.descriptor_count(desc_count),
    .group_is_128(group_size==32'd128),.busy(dp_busy),.token_done(dp_done),.error(dp_error),.logits_bus(logits_q),.argmax_index(argmax_q),
    .desc_mem_req_valid(dqv),.desc_mem_req_ready(dqr),.desc_mem_req_addr(dqa),.desc_mem_rsp_valid(drv),.desc_mem_rsp_ready(drr),.desc_mem_rsp_rdata(drd),
    .dma_mem_req_valid(xqv),.dma_mem_req_ready(xqr),.dma_mem_req_addr(xqa),.dma_mem_rsp_valid(xrv),.dma_mem_rsp_ready(xrr),.dma_mem_rsp_rdata(xrd));
  always_comb begin
    logits_i32='0; for (i=0;i<512;i=i+1) logits_i32[i*32 +: 32]={{(32-DATA_W){logits_q[i*DATA_W+DATA_W-1]}},logits_q[i*DATA_W +: DATA_W]};
  end
  logit_writer writer (.clk,.rst_n,.start_row(write_row_valid),.write_status_after_row(write_final_status),.logits_base_addr(logits_addr),
    .argmax_base_addr(argmax_addr),.status_addr,.logit_row_stride(logit_stride),.token_index(write_token_index),.logits_bus(logits_i32),
    .argmax_id({23'd0,argmax_q}),.busy(writer_busy),.row_done(writer_row_done),.run_done(writer_run_done),.mem_req_valid(wqv),.mem_req_ready(wqr),
    .mem_req_write(wqwr),.mem_req_addr(wqa),.mem_req_wdata(wqd),.mem_req_wstrb(wqs));

  always_comb begin
    tqv=(token_state==TR_REQ); tqa=tokens_addr+((token_index>>2)<<4); trr=(token_state==TR_WAIT); token_read_done=(token_state==TR_DONE);
    hqr=a0qr&&hqv; tqr=a0qr&&!hqv; hrv=a0rv&&header_busy; trv=a0rv&&!header_busy; hrd=a0rd; trd=a0rd;
    a0qv=hqv||tqv; a0rr=header_busy?hrr:trr;
    busy=(top_state!=IDLE&&top_state!=DONE&&top_state!=ERROR); done=(top_state==DONE); error=(top_state==ERROR);
    header_start=(top_state==HDR_START); write_row_done=writer_row_done||writer_run_done;
  end
  always_ff @(posedge clk or negedge rst_n) begin
    if(!rst_n) begin token_state<=TR_IDLE; token_id<='0; end else case(token_state)
      TR_IDLE: if(token_read_valid) token_state<=TR_REQ;
      TR_REQ: if(tqr) token_state<=TR_WAIT;
      TR_WAIT: if(trv) begin token_id<=trd[token_index[1:0]*32 +: 32]; token_state<=TR_DONE; end
      TR_DONE: if(!token_read_valid) token_state<=TR_IDLE;
      default: token_state<=TR_IDLE;
    endcase
  end
  always_ff @(posedge clk or negedge rst_n) begin
    if(!rst_n) top_state<=IDLE; else case(top_state)
      IDLE: if(run) top_state<=HDR_START;
      HDR_START: top_state<=HDR_WAIT;
      HDR_WAIT: if(header_done) begin if(header_error||n_layers!=2||d_model!=64||vocab!=512||seq_len==0||seq_len>MAX_SEQ||(group_size!=64&&group_size!=128)) top_state<=ERROR; else top_state<=EXEC_START; end
      EXEC_START: top_state<=EXEC_WAIT;
      EXEC_WAIT: if(ctrl_error||dp_error) top_state<=ERROR; else if(ctrl_done) top_state<=DONE;
      DONE,ERROR: top_state<=IDLE; default: top_state<=IDLE;
    endcase
  end
  mem_arbiter4 arb (.clk,.rst_n,
    .m0_req_valid(a0qv),.m0_req_write(1'b0),.m0_req_addr(hqv?hqa:tqa),.m0_req_wdata('0),.m0_req_wstrb('0),.m0_req_ready(a0qr),.m0_rsp_valid(a0rv),.m0_rsp_ready(a0rr),.m0_rsp_rdata(a0rd),
    .m1_req_valid(dqv),.m1_req_write(1'b0),.m1_req_addr(dqa),.m1_req_wdata('0),.m1_req_wstrb('0),.m1_req_ready(dqr),.m1_rsp_valid(drv),.m1_rsp_ready(drr),.m1_rsp_rdata(drd),
    .m2_req_valid(xqv),.m2_req_write(1'b0),.m2_req_addr(xqa),.m2_req_wdata('0),.m2_req_wstrb('0),.m2_req_ready(xqr),.m2_rsp_valid(xrv),.m2_rsp_ready(xrr),.m2_rsp_rdata(xrd),
    .m3_req_valid(wqv),.m3_req_write(wqwr),.m3_req_addr(wqa),.m3_req_wdata(wqd),.m3_req_wstrb(wqs),.m3_req_ready(wqr),.m3_rsp_valid(),.m3_rsp_ready(1'b1),.m3_rsp_rdata(wrd),
    .mem_req_valid,.mem_req_ready,.mem_req_write,.mem_req_addr,.mem_req_wdata,.mem_req_wstrb,.mem_rsp_valid,.mem_rsp_ready,.mem_rsp_rdata);
endmodule
