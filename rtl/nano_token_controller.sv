// Independent token-level schedule for the two-layer nano architecture.
// Each phase starts an external datapath/weight-stream operation and waits
// for stage_done.  This makes all causal boundaries explicit.
module nano_token_controller (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       token_start,
  input  logic       stage_done,
  output logic       stage_valid,
  output logic [4:0] stage_id,
  output logic       token_done
);
  localparam logic [4:0]
    IDLE       = 5'd0,
    EMB        = 5'd1,
    L0_NORM1   = 5'd2,
    L0_KDA     = 5'd3,
    L0_RESMIX  = 5'd4,
    L0_NORM2   = 5'd5,
    L0_MOE     = 5'd6,
    L1_RESMIXA = 5'd7,
    L1_NORM1   = 5'd8,
    L1_MLA     = 5'd9,
    L1_RESMIXM = 5'd10,
    L1_NORM2   = 5'd11,
    L1_MOE     = 5'd12,
    OUT_RESMIX = 5'd13,
    FINAL_NORM = 5'd14,
    LM_HEAD    = 5'd15,
    DONE       = 5'd16;

  logic [4:0] phase, next_phase;

  always_comb begin
    next_phase = phase;
    case (phase)
      IDLE:       if (token_start) next_phase = EMB;
      EMB:        if (stage_done) next_phase = L0_NORM1;
      L0_NORM1:   if (stage_done) next_phase = L0_KDA;
      L0_KDA:     if (stage_done) next_phase = L0_RESMIX;
      L0_RESMIX:  if (stage_done) next_phase = L0_NORM2;
      L0_NORM2:   if (stage_done) next_phase = L0_MOE;
      L0_MOE:     if (stage_done) next_phase = L1_RESMIXA;
      L1_RESMIXA: if (stage_done) next_phase = L1_NORM1;
      L1_NORM1:   if (stage_done) next_phase = L1_MLA;
      L1_MLA:     if (stage_done) next_phase = L1_RESMIXM;
      L1_RESMIXM: if (stage_done) next_phase = L1_NORM2;
      L1_NORM2:   if (stage_done) next_phase = L1_MOE;
      L1_MOE:     if (stage_done) next_phase = OUT_RESMIX;
      OUT_RESMIX: if (stage_done) next_phase = FINAL_NORM;
      FINAL_NORM: if (stage_done) next_phase = LM_HEAD;
      LM_HEAD:    if (stage_done) next_phase = DONE;
      DONE:       next_phase = IDLE;
      default:    next_phase = IDLE;
    endcase
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)
      phase <= IDLE;
    else
      phase <= next_phase;
  end

  assign stage_id = phase;
  assign stage_valid = (phase != IDLE) && (phase != DONE);
  assign token_done = (phase == DONE);
endmodule
