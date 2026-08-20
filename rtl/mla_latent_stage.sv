// NoPE MLA's latent and query/key packing boundary.
// `wc_pre`, `qc_pre`, `qr_pre` and `kr_pre` are q4 matvec results.  The
// caller uses `latent_bus` as the input for wk_c/wv q4 matvecs, then passes
// those results into mla_cache_attention_contract.
module mla_latent_stage #(
  parameter int DATA_W = 24, parameter int FRAC_W = 14
) (
  input logic signed [128*DATA_W-1:0] wc_pre_bus,
  input logic signed [128*16-1:0] knorm_offset_bus,
  input logic signed [64*DATA_W-1:0] qc_pre_bus,
  input logic signed [32*DATA_W-1:0] qr_pre_bus,
  input logic signed [16*DATA_W-1:0] kr_pre_bus,
  output logic signed [128*DATA_W-1:0] latent_bus,
  output logic signed [2*48*DATA_W-1:0] query_bus,
  output logic signed [2*16*DATA_W-1:0] shared_kr_bus
);
  integer h, i;
  rmsnorm_q #(.DIM(128), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) latent_norm
    (.x_bus(wc_pre_bus), .norm_offset_bus(knorm_offset_bus), .y_bus(latent_bus));
  always_comb begin
    query_bus = '0; shared_kr_bus = '0;
    for (h = 0; h < 2; h = h + 1) begin
      for (i = 0; i < 32; i = i + 1)
        query_bus[(h*48+i)*DATA_W +: DATA_W] = qc_pre_bus[(h*32+i)*DATA_W +: DATA_W];
      for (i = 0; i < 16; i = i + 1) begin
        query_bus[(h*48+32+i)*DATA_W +: DATA_W] = qr_pre_bus[(h*16+i)*DATA_W +: DATA_W];
        shared_kr_bus[(h*16+i)*DATA_W +: DATA_W] = kr_pre_bus[i*DATA_W +: DATA_W];
      end
    end
  end
endmodule
