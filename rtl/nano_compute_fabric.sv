// Token-by-token micro-operation sequencer for the independent nano decoder.
//
// A micro-op is executed only after its operand set has been fetched from the
// image. `weight_job` is the control-plane boundary to descriptor_lookup +
// dma_read16; `op_*` is the data-plane boundary to the activation datapath.
// Keeping these handshakes explicit allows a correctness-first serialized
// implementation now and a multi-engine/prefetch implementation later.
module nano_compute_fabric (
  input  logic        clk,
  input  logic        rst_n,
  input  logic        start_run,
  input  logic [31:0] seq_len,
  output logic        busy,
  output logic        run_done,

  // Token input side: returns the teacher-forced token for the current pos.
  output logic        token_read_valid,
  output logic [31:0] token_index,
  input  logic        token_read_done,

  // Weight front-end side. The front-end must fetch every q/s/z/small tensor
  // listed by this job, place it into the datapath's weight buffer, then ack.
  output logic        weight_job_valid,
  output logic [4:0]  weight_job,
  output logic [31:0] weight_token_index,
  input  logic        weight_job_done,

  // Activation datapath side. op_done means its output activation has been
  // committed to the named activation bank / KDA state / MLA cache.
  output logic        op_valid,
  output logic [4:0]  op_code,
  output logic [31:0] op_token_index,
  input  logic        op_done,

  // Output side: writer starts only after HEAD/argmax have committed.
  output logic        write_row_valid,
  output logic [31:0] write_token_index,
  output logic        write_final_status,
  input  logic        write_row_done
);
  localparam logic [4:0]
    OP_EMBED       = 5'd0,
    OP_L0_NORM1    = 5'd1,
    OP_L0_KDA      = 5'd2,
    OP_L0_RESMIX   = 5'd3,
    OP_L0_NORM2    = 5'd4,
    OP_L0_MOE      = 5'd5,
    OP_L1_RESA     = 5'd6,
    OP_L1_NORM1    = 5'd7,
    OP_L1_MLA      = 5'd8,
    OP_L1_RESMIX   = 5'd9,
    OP_L1_NORM2    = 5'd10,
    OP_L1_MOE      = 5'd11,
    OP_OUT_RESMIX  = 5'd12,
    OP_FINAL_NORM  = 5'd13,
    OP_HEAD_ARGMAX = 5'd14;

  typedef enum logic [2:0] {IDLE, READ_TOKEN, FETCH_WEIGHTS, EXECUTE, WRITE_ROW, FINISH} state_t;
  state_t state;
  logic [4:0] current_op;
  logic [31:0] token_pos;

  function automatic logic [4:0] next_op(input logic [4:0] op);
    case (op)
      OP_EMBED:       next_op = OP_L0_NORM1;
      OP_L0_NORM1:    next_op = OP_L0_KDA;
      OP_L0_KDA:      next_op = OP_L0_RESMIX;
      OP_L0_RESMIX:   next_op = OP_L0_NORM2;
      OP_L0_NORM2:    next_op = OP_L0_MOE;
      OP_L0_MOE:      next_op = OP_L1_RESA;
      OP_L1_RESA:     next_op = OP_L1_NORM1;
      OP_L1_NORM1:    next_op = OP_L1_MLA;
      OP_L1_MLA:      next_op = OP_L1_RESMIX;
      OP_L1_RESMIX:   next_op = OP_L1_NORM2;
      OP_L1_NORM2:    next_op = OP_L1_MOE;
      OP_L1_MOE:      next_op = OP_OUT_RESMIX;
      OP_OUT_RESMIX:  next_op = OP_FINAL_NORM;
      OP_FINAL_NORM:  next_op = OP_HEAD_ARGMAX;
      default:        next_op = OP_EMBED;
    endcase
  endfunction

  always_comb begin
    busy = (state != IDLE) && (state != FINISH);
    run_done = (state == FINISH);
    token_read_valid = (state == READ_TOKEN);
    token_index = token_pos;
    weight_job_valid = (state == FETCH_WEIGHTS);
    weight_job = current_op;
    weight_token_index = token_pos;
    op_valid = (state == EXECUTE);
    op_code = current_op;
    op_token_index = token_pos;
    write_row_valid = (state == WRITE_ROW);
    write_token_index = token_pos;
    write_final_status = (token_pos + 1 >= seq_len);
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE;
      current_op <= OP_EMBED;
      token_pos <= '0;
    end else begin
      case (state)
        IDLE: if (start_run) begin
          token_pos <= '0;
          current_op <= OP_EMBED;
          state <= READ_TOKEN;
        end
        READ_TOKEN: if (token_read_done) state <= FETCH_WEIGHTS;
        FETCH_WEIGHTS: if (weight_job_done) state <= EXECUTE;
        EXECUTE: if (op_done) begin
          if (current_op == OP_HEAD_ARGMAX) state <= WRITE_ROW;
          else begin
            current_op <= next_op(current_op);
            state <= FETCH_WEIGHTS;
          end
        end
        WRITE_ROW: if (write_row_done) begin
          if (token_pos + 1 >= seq_len)
            state <= FINISH;
          else begin
            token_pos <= token_pos + 1'b1;
            current_op <= OP_EMBED;
            state <= READ_TOKEN;
          end
        end
        FINISH: state <= IDLE;
        default: state <= IDLE;
      endcase
    end
  end
endmodule
