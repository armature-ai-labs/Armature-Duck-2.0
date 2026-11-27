# Importing Custom Motion Models

Custom ONNX policies can be imported through the **Models** card on the **More** page or via `POST /api/models/import`.

## Package Format

- `.zip` archive or single `.onnx` file.
- Optional `deployment-contract.json` and `deployment-execution.json` alongside `policy.onnx` to declare `kp_fw`, `action_scale`, and target filters.
- Slot assignment supports `walk` and skill slots (`pickup`, `roll`, `kick_r`, `kick_l`, etc.).

## Safety

- Importing or activating models requires torque-off and no active walking policy.
- `robotd` validates contracts before loading any policy into the 50 Hz control loop.
