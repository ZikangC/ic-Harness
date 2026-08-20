`timescale 1ns/1ps
module tb_kda_delta_head;
  localparam int W=24, F=14, D=32;
  logic clk=0, rst_n=0, step_valid=0;
  logic signed [D*W-1:0] q_in,k_in,v_in,alpha_in,out_bus;
  logic signed [W-1:0] beta_in;
  integer i;
  kda_delta_head #(.DH(D),.DATA_W(W),.FRAC_W(F)) dut (.*);
  always #5 clk=~clk;
  initial begin
    q_in='0; k_in='0; v_in='0; alpha_in='0; beta_in=8192;
    // One-hot q/k makes the expected token-0 result exact and exposes both
    // state orientation and the read from the updated state.
    q_in[0 +: W]=16384; k_in[0 +: W]=16384;
    for(i=0;i<D;i=i+1) v_in[i*W +: W]=(i+1)*1024;
    repeat(2) @(posedge clk); rst_n=1; step_valid=1; #1;
    $write("BEFORE"); for(i=0;i<D;i=i+1) $write(" %0d",$signed(out_bus[i*W +: W])); $write("\n");
    @(posedge clk); #1; step_valid=0;
    $display("STATE next=%0d state=%0d err=%0d beta_key=%0d", dut.next_state[0][0], dut.state[0][0], dut.error[0], dut.beta_key);
    $write("AFTER"); for(i=0;i<D;i=i+1) $write(" %0d",$signed(out_bus[i*W +: W])); $write("\n");
    $finish;
  end
endmodule
