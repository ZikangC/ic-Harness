`timescale 1ns/1ps
module tb_nano_golden_decoder_top;
  // Prompt 1 has 16 tokens; allow its complete state/cache audit plus
  // writes without turning a legitimate long run into a false timeout.
  localparam int MEM_BYTES=262144, MAX_CYCLES=1500000;
  logic clk=0,rst_n=0,run=0,busy,done,error,mem_req_valid,mem_req_ready,mem_req_write,mem_rsp_valid,mem_rsp_ready;
  logic [35:0] mem_req_addr; logic [127:0] mem_req_wdata,mem_rsp_rdata; logic [15:0] mem_req_wstrb;
  logic [7:0] memory[0:MEM_BYTES-1]; logic rsp_pending,saw_status; integer i,cycles,fd; string image_path,dump_path;
  logic [35:0] status_addr;
  nano_golden_decoder_top dut (.clk,.rst_n,.run,.busy,.done,.error,.mem_req_valid,.mem_req_ready,.mem_req_write,.mem_req_addr,.mem_req_wdata,.mem_req_wstrb,.mem_rsp_valid,.mem_rsp_ready,.mem_rsp_rdata);
  always #5 clk=~clk;
  assign mem_req_ready=!rsp_pending;
  always_ff @(posedge clk) begin
    if(!rst_n) begin mem_rsp_valid<=0; mem_rsp_rdata<='0; rsp_pending<=0; saw_status<=0; end else begin
      mem_rsp_valid<=0;
      if(rsp_pending&&mem_rsp_ready) rsp_pending<=0;
      if(mem_req_valid&&mem_req_ready) begin
        if(mem_req_write) begin
          for(i=0;i<16;i=i+1) if(mem_req_wstrb[i]) memory[mem_req_addr+i]<=mem_req_wdata[i*8 +: 8];
          if(mem_req_addr==status_addr&&mem_req_wdata[31:0]==32'h600d_d00d) saw_status<=1;
        end else begin
          for(i=0;i<16;i=i+1) mem_rsp_rdata[i*8 +: 8]<=memory[mem_req_addr+i];
          rsp_pending<=1;
        end
      end
      if(rsp_pending) mem_rsp_valid<=1;
    end
  end
  task automatic dump_memory;
    begin
      if($value$plusargs("DUMP=%s",dump_path)) begin
        fd=$fopen(dump_path,"wb");
        for(i=0;i<MEM_BYTES;i=i+1) $fwrite(fd,"%c",memory[i]);
        $fclose(fd);
      end
    end
  endtask
  task automatic dump_q14_vec(input string name, input logic signed [64*24-1:0] vec);
    begin
      $write("RTL_Q14 %s", name);
      for (i=0;i<64;i=i+1) $write(" %0d", $signed(vec[i*24 +: 24]));
      $write("\n");
    end
  endtask
  task automatic dump_q14_vec8(input string name, input logic signed [8*24-1:0] vec);
    begin
      $write("RTL_Q14_8 %s", name);
      for (i=0;i<8;i=i+1) $write(" %0d", $signed(vec[i*24 +: 24]));
      $write("\n");
    end
  endtask
  task automatic dump_token_q14(input integer pos, input string name, input logic signed [64*24-1:0] vec);
    begin
      $write("TOKEN_Q14 pos=%0d %s", pos, name);
      for (i=0;i<64;i=i+1) $write(" %0d", $signed(vec[i*24 +: 24]));
      $write("\n");
    end
  endtask
  task automatic dump_token_q14_32(input integer pos, input string name, input logic signed [32*24-1:0] vec);
    begin
      $write("TOKEN_Q14_32 pos=%0d %s", pos, name);
      for (i=0;i<32;i=i+1) $write(" %0d", $signed(vec[i*24 +: 24]));
      $write("\n");
    end
  endtask
  task automatic dump_token_q14_8(input integer pos, input string name, input logic signed [8*24-1:0] vec);
    begin
      $write("TOKEN_Q14_8 pos=%0d %s", pos, name);
      for (i=0;i<8;i=i+1) $write(" %0d", $signed(vec[i*24 +: 24]));
      $write("\n");
    end
  endtask
  initial begin
    if(!$value$plusargs("IMAGE=%s",image_path)) $fatal(1,"IMAGE is required");
    for(i=0;i<MEM_BYTES;i=i+1) memory[i]=0;
    $readmemh(image_path,memory);
    status_addr={memory[111],memory[110],memory[109],memory[108]};
    repeat(5) @(posedge clk); rst_n=1; @(posedge clk); run=1; @(posedge clk); run=0;
    for(cycles=0;cycles<MAX_CYCLES;cycles=cycles+1) begin
      @(posedge clk);
      if ((cycles % 1000) == 0)
        $display("PROGRESS cycle=%0d top=%0d ctrl=%0d token=%0d dp=%0d writer=%0d", cycles,
          dut.top_state, dut.ctrl.state, dut.token_state, dut.dp.state, dut.writer.state);
      if(error) begin dump_memory(); $fatal(1,"golden top error at cycle %0d",cycles); end
      // State-facing multi-token audit.  These are sampled only at the
      // architectural token commit, after KDA state/conv history and the
      // MLA cache write have committed, before the controller advances.
      if (dut.dp.token_done) begin
        $display("TOKEN_AUDIT pos=%0d L0_ROUTE=%0d,%0d L1_ROUTE=%0d,%0d KDA_QSIG=%08x,%08x,%08x",
          dut.dp.token_position, dut.dp.l0_selected0, dut.dp.l0_selected1,
          dut.dp.l1_selected0, dut.dp.l1_selected1,
          dut.dp.qpre_qsig, dut.dp.kpre_qsig, dut.dp.vpre_qsig);
        dump_token_q14(dut.dp.token_position, "kda_core", dut.dp.kda_core_reg);
        dump_token_q14(dut.dp.token_position, "kda_attention", dut.dp.kda_attention);
        dump_token_q14(dut.dp.token_position, "l0_moe_norm", dut.dp.l0_moe_norm);
        dump_token_q14_8(dut.dp.token_position, "l0_router_pre", dut.dp.l0_router_pre);
        // These are the exact current-token inputs of MLA's cache WRITE:
        // key=[kc, shared_kr] per head and the two-head value vector.
        dump_token_q14(dut.dp.token_position, "mla_write_kc", dut.dp.l1_kc);
        dump_token_q14_32(dut.dp.token_position, "mla_write_shared_kr", dut.dp.l1_shared_kr);
        dump_token_q14(dut.dp.token_position, "mla_write_value", dut.dp.l1_value);
        dump_token_q14(dut.dp.token_position, "mla_context", dut.dp.l1_context);
      end
      if(done) begin
        dump_memory();
        if(!saw_status) $fatal(1,"done without status");
        dump_q14_vec("embedding", dut.dp.x_bus);
        dump_q14_vec("h_norm", dut.dp.h_norm_reg);
        dump_q14_vec("q_pre", dut.dp.q_pre);
        dump_q14_vec("k_pre", dut.dp.k_pre);
        dump_q14_vec("v_pre", dut.dp.v_pre);
        $display("KDA_QSIG q=%08x k=%08x v=%08x", dut.dp.qpre_qsig, dut.dp.kpre_qsig, dut.dp.vpre_qsig);
        dump_q14_vec("f_reg", dut.dp.f_reg);
        dump_q14_vec("z_reg", dut.dp.z_reg);
        dump_q14_vec("kda_qconv", dut.dp.kda_qconv_dbg);
        dump_q14_vec("kda_kconv", dut.dp.kda_kconv_dbg);
        dump_q14_vec("kda_vconv", dut.dp.kda_vconv_dbg);
        dump_q14_vec("kda_qact", dut.dp.kda_qact_dbg);
        dump_q14_vec("kda_kact", dut.dp.kda_kact_dbg);
        dump_q14_vec("kda_vact", dut.dp.kda_vact_dbg);
        dump_q14_vec("kda_qnorm", dut.dp.kda_qnorm_dbg);
        dump_q14_vec("kda_knorm", dut.dp.kda_knorm_dbg);
        dump_q14_vec("kda_alpha", dut.dp.kda_alpha_dbg);
        $display("RTL_Q14 kda_beta %0d %0d", $signed(dut.dp.kda_beta_dbg[0 +: 24]), $signed(dut.dp.kda_beta_dbg[24 +: 24]));
        dump_q14_vec("kda_head_raw", dut.dp.kda_head_raw_dbg);
        dump_q14_vec("kda_head_norm", dut.dp.kda_head_norm_dbg);
        dump_q14_vec("kda_core", dut.dp.kda_core_reg);
        dump_q14_vec("kda_attention", dut.dp.kda_attention);
        dump_q14_vec("l0_mlp_mix", dut.dp.l0_mlp_mix);
        dump_q14_vec("l0_moe_norm", dut.dp.l0_moe_norm);
        dump_q14_vec8("l0_router_pre", dut.dp.l0_router_pre);
        dump_q14_vec("l0_shared_expert", dut.dp.shared_expert);
        dump_q14_vec("l0_selected_expert0", dut.dp.selected_expert0);
        dump_q14_vec("l0_selected_expert1", dut.dp.selected_expert1);
        dump_q14_vec("l0_moe_out", dut.dp.l0_moe_out);
        dump_q14_vec("l0_out", dut.dp.l0_out);
        dump_q14_vec("l1_attn_mixed", dut.dp.l1_attn_mixed);
        dump_q14_vec("l1_norm1", dut.dp.l1_norm1);
        dump_q14_vec("l1_kc", dut.dp.l1_kc);
        dump_q14_vec("l1_value", dut.dp.l1_value);
        dump_q14_vec("l1_context", dut.dp.l1_context);
        dump_q14_vec("l1_mla_attention", dut.dp.l1_mla_attention);
        dump_q14_vec("l1_prefix", dut.dp.l1_prefix);
        dump_q14_vec("l1_mlp_mix", dut.dp.l1_mlp_mix);
        dump_q14_vec("l1_moe_norm", dut.dp.l1_moe_norm);
        dump_q14_vec8("l1_router_pre", dut.dp.l1_router_pre_reg);
        $display("L1_ROUTE pre0=%0d pre1=%0d reg0=%0d reg1=%0d sel=%0d,%0d weight=%0d,%0d",
          $signed(dut.dp.l1_router_pre[0 +: 24]), $signed(dut.dp.l1_router_pre[24 +: 24]),
          $signed(dut.dp.l1_router_pre_reg[0 +: 24]), $signed(dut.dp.l1_router_pre_reg[24 +: 24]),
          dut.dp.l1_selected0, dut.dp.l1_selected1, $signed(dut.dp.l1_route_w0), $signed(dut.dp.l1_route_w1));
        dump_q14_vec("l1_shared_expert", dut.dp.l1_shared_expert);
        dump_q14_vec("l1_selected_expert0", dut.dp.l1_selected_expert0);
        dump_q14_vec("l1_selected_expert1", dut.dp.l1_selected_expert1);
        dump_q14_vec("l1_moe_out", dut.dp.l1_moe_out);
        dump_q14_vec("l1_out", dut.dp.l1_out);
        dump_q14_vec("out_res_mix", dut.dp.out_res_mix);
        dump_q14_vec("final_hidden", dut.dp.final_hidden);
        $display("PASS cycles=%0d",cycles);
        $finish;
      end
    end
    dump_memory(); $fatal(1,"timeout after %0d cycles",MAX_CYCLES);
  end
endmodule
