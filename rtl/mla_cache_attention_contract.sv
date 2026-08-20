// Causal NoPE MLA cache plus both attention heads. The current k/v is written
// before the two scans, therefore the attention domain is positions [0..pos].
module mla_cache_attention_contract #(
  parameter int MAX_SEQ = 64, parameter int DATA_W = 24, parameter int FRAC_W = 14
) (
  input logic clk, input logic rst_n, input logic start,
  input logic [$clog2(MAX_SEQ)-1:0] position,
  input logic signed [2*48*DATA_W-1:0] query_bus,
  input logic signed [64*DATA_W-1:0] kc_bus,
  input logic signed [2*16*DATA_W-1:0] shared_kr_bus,
  input logic signed [64*DATA_W-1:0] value_bus,
  output logic busy, output logic done,
  output logic signed [64*DATA_W-1:0] context_bus
);
  typedef enum logic [1:0] {IDLE, WRITE, ATTEND, FINISH} state_t;
  state_t state;
  logic write_valid, a0_start, a1_start, a0_busy, a1_busy, a0_done, a1_done;
  logic [$clog2(MAX_SEQ)-1:0] read_pos, a0_index, a1_index;
  logic signed [2*48*DATA_W-1:0] key_write, key_read;
  logic signed [2*32*DATA_W-1:0] value_read;
  logic signed [32*DATA_W-1:0] c0, c1;
  integer h, i;
  always_comb begin
    key_write = '0;
    for (h = 0; h < 2; h = h + 1) begin
      for (i = 0; i < 32; i = i + 1) key_write[(h*48+i)*DATA_W +: DATA_W] = kc_bus[(h*32+i)*DATA_W +: DATA_W];
      for (i = 0; i < 16; i = i + 1) key_write[(h*48+32+i)*DATA_W +: DATA_W] = shared_kr_bus[(h*16+i)*DATA_W +: DATA_W];
    end
    read_pos = a0_index;
    write_valid = (state == WRITE);
    a0_start = (state == ATTEND) && !a0_busy && !a0_done;
    a1_start = (state == ATTEND) && !a1_busy && !a1_done;
    busy = (state == WRITE) || (state == ATTEND);
    done = (state == FINISH);
    context_bus = {c1, c0};
  end
  mla_kv_cache #(.MAX_SEQ(MAX_SEQ), .HEADS(2), .KD(48), .DV(32), .DATA_W(DATA_W)) cache (
    .clk, .rst_n, .write_valid, .write_pos(position), .write_key_bus(key_write), .write_value_bus(value_bus),
    .read_pos, .read_key_bus(key_read), .read_value_bus(value_read));
  mla_attention_head #(.MAX_SEQ(MAX_SEQ), .KD(48), .DV(32), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) head0 (
    .clk, .rst_n, .start(a0_start), .valid_count({1'b0,position}+1'b1), .query_bus(query_bus[48*DATA_W-1:0]),
    .cache_index(a0_index), .cache_key_bus(key_read[48*DATA_W-1:0]), .cache_value_bus(value_read[32*DATA_W-1:0]),
    .busy(a0_busy), .done(a0_done), .context_bus(c0));
  mla_attention_head #(.MAX_SEQ(MAX_SEQ), .KD(48), .DV(32), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) head1 (
    .clk, .rst_n, .start(a1_start), .valid_count({1'b0,position}+1'b1), .query_bus(query_bus[48*DATA_W +: 48*DATA_W]),
    .cache_index(a1_index), .cache_key_bus(key_read[48*DATA_W +: 48*DATA_W]), .cache_value_bus(value_read[32*DATA_W +: 32*DATA_W]),
    .busy(a1_busy), .done(a1_done), .context_bus(c1));
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) state <= IDLE;
    else case (state)
      IDLE: if (start) state <= WRITE;
      WRITE: state <= ATTEND;
      ATTEND: if (a0_done && a1_done) state <= FINISH;
      FINISH: state <= IDLE;
      default: state <= IDLE;
    endcase
  end
endmodule
