// One MLA head's causal attention. It scans cache positions 0..valid_count-1
// twice: first q.K scores, then softmax-weighted V accumulation.
module mla_attention_head #(
  parameter int MAX_SEQ = 64,
  parameter int KD      = 48,
  parameter int DV      = 32,
  parameter int DATA_W  = 24,
  parameter int FRAC_W  = 14,
  localparam int POS_W  = $clog2(MAX_SEQ)
) (
  input  logic                        clk,
  input  logic                        rst_n,
  input  logic                        start,
  input  logic [POS_W:0]              valid_count,
  input  logic signed [KD*DATA_W-1:0] query_bus,
  output logic [POS_W-1:0]            cache_index,
  input  logic signed [KD*DATA_W-1:0] cache_key_bus,
  input  logic signed [DV*DATA_W-1:0] cache_value_bus,
  output logic                        busy,
  output logic                        done,
  output logic signed [DV*DATA_W-1:0] context_bus
);
  typedef enum logic [2:0] {IDLE, SCORE, NORM_WAIT, VALUE, FINISH} state_t;
  state_t state;
  logic [POS_W-1:0] index;
  logic signed [DATA_W-1:0] score_mem [0:MAX_SEQ-1];
  logic signed [MAX_SEQ*DATA_W-1:0] score_bus;
  logic [MAX_SEQ*15-1:0] probability_bus;
  logic signed [63:0] value_accum [0:DV-1];
  logic signed [63:0] dot;
  logic signed [DATA_W-1:0] q_value, k_value, v_value;
  logic [14:0] probability;
  integer d, p;

  // softmax observes score_mem after the score phase's final registered write.
  always_comb begin
    score_bus = '0;
    for (p = 0; p < MAX_SEQ; p = p + 1)
      score_bus[p*DATA_W +: DATA_W] = score_mem[p];
  end
  softmax_vector_pwl #(.MAX_ELEMS(MAX_SEQ), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) softmax_unit (
    .count(valid_count), .score_bus(score_bus), .probability_bus(probability_bus)
  );

  always_comb begin
    busy = (state != IDLE) && (state != FINISH);
    done = (state == FINISH);
    cache_index = index;
    context_bus = '0;
    for (d = 0; d < DV; d = d + 1)
      context_bus[d*DATA_W +: DATA_W] = value_accum[d] >>> FRAC_W;
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE;
      index <= '0;
      for (p = 0; p < MAX_SEQ; p = p + 1) score_mem[p] <= '0;
      for (d = 0; d < DV; d = d + 1) value_accum[d] <= '0;
    end else begin
      case (state)
        IDLE: if (start) begin
          index <= '0;
          for (d = 0; d < DV; d = d + 1) value_accum[d] <= '0;
          state <= SCORE;
        end
        SCORE: begin
          dot = 0;
          // Accumulate the full-precision Q2*FRAC products and rescale once.
          // The previous per-term `(q*k) >>> FRAC_W` truncation biased every
          // attention logit downward by up to 0.5 LSB; over 48 key dims this
          // systematic bias shifts the softmax probabilities and, through the
          // MLA context, the L1 router near-ties.  The single 29-bit shift
          // below produces the same Q*.14 score scale as before.
          for (d = 0; d < KD; d = d + 1) begin
            q_value = $signed(query_bus[d*DATA_W +: DATA_W]);
            k_value = $signed(cache_key_bus[d*DATA_W +: DATA_W]);
            dot = dot + q_value * k_value;
          end
          // 1/sqrt(48) ~= 4729 / 2^15; rescale the Q2*FRAC dot to Q*.14.
          score_mem[index] <= (dot * 4729) >>> (FRAC_W + 15);
          if (index + 1 >= valid_count) state <= NORM_WAIT;
          else index <= index + 1'b1;
        end
        NORM_WAIT: begin
          index <= '0;
          state <= VALUE;
        end
        VALUE: begin
          probability = probability_bus[index*15 +: 15];
          for (d = 0; d < DV; d = d + 1) begin
            v_value = $signed(cache_value_bus[d*DATA_W +: DATA_W]);
            value_accum[d] <= value_accum[d] + v_value * $signed({1'b0, probability});
          end
          if (index + 1 >= valid_count) state <= FINISH;
          else index <= index + 1'b1;
        end
        FINISH: state <= IDLE;
        default: state <= IDLE;
      endcase
    end
  end
endmodule
