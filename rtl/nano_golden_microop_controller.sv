// Generic sequential control plane for the golden decoder.  A datapath-side
// microcode table supplies the next named tensor load and reports each compute
// commit. This controller owns only run/token/write sequencing, so it never
// reintroduces the legacy private descriptor ABI.
module nano_golden_microop_controller (
  input logic clk, input logic rst_n, input logic start_run,
  input logic [31:0] seq_len,
  output logic busy, output logic run_done,
  output logic token_read_valid, output logic [31:0] token_index,
  input logic token_read_done,
  output logic begin_token,
  input logic datapath_busy, input logic datapath_token_done, input logic datapath_error,
  output logic write_row_valid, output logic [31:0] write_token_index,
  output logic write_final_status, input logic write_row_done,
  output logic error
);
  typedef enum logic [2:0] {IDLE, TOKEN_READ, TOKEN_START, TOKEN_EXEC, WRITE, FINISH, FAILED} state_t;
  state_t state;
  logic [31:0] position;
  always_comb begin
    busy = !(state == IDLE || state == FINISH || state == FAILED);
    run_done = (state == FINISH);
    error = (state == FAILED);
    token_read_valid = (state == TOKEN_READ);
    token_index = position;
    begin_token = (state == TOKEN_START);
    write_row_valid = (state == WRITE);
    write_token_index = position;
    write_final_status = (position + 1 >= seq_len);
  end
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin state <= IDLE; position <= '0; end
    else case (state)
      IDLE: if (start_run) begin position <= '0; state <= (seq_len == 0) ? FAILED : TOKEN_READ; end
      TOKEN_READ: if (token_read_done) state <= TOKEN_START;
      TOKEN_START: state <= TOKEN_EXEC;
      TOKEN_EXEC: if (datapath_error) state <= FAILED;
                  else if (datapath_token_done && !datapath_busy) state <= WRITE;
      WRITE: if (write_row_done) begin
        if (position + 1 >= seq_len) state <= FINISH;
        else begin position <= position + 1'b1; state <= TOKEN_READ; end
      end
      FINISH, FAILED: state <= IDLE;
      default: state <= IDLE;
    endcase
  end
endmodule
