# RoomTrace workflow

## Capture route

Start in the centre of the room and wait for `TRACKING`. Walk the perimeter at a slow pace, then revisit furniture and wall details from a second angle. Finish by looking at the start area again. Capture the floor and ceiling deliberately; do not rely on one quick sweep.

Good capture conditions:

- diffuse, steady light;
- phone held roughly chest height and moved slowly;
- textured surfaces visible in more than one direction;
- mirrors, windows, glossy black surfaces, and people kept out of the path when possible.

## Process route

```powershell
roomtrace inspect .\capture.roomcap.zip --verify-checksums
roomtrace process .\capture.roomcap.zip --output .\room-output
```

For a very large capture, increase `--depth-step` from `4` to `6` or `8` to reduce the mesh size. Lower it to `2` for a detail pass. `--clean-voxel 0.025` means the Clean model is reduced to roughly 2.5cm cells; the Raw model is not voxel-reduced.

The optional `--loop-closure` flag distributes a positional correction between the first and last pose. Use it only when the capture really returns to the initial position. It does not invent a correction automatically.

If one room dimension is known, pass it as a scale anchor, for example:

```powershell
roomtrace process .\capture.roomcap.zip --output .\room-output --reference-width 4.85
```

When both width and depth are supplied, RoomTrace rejects them if they imply materially different scales instead of silently stretching the model.

## Blender route

Import `room_reference_clean.glb`, create the modeled room in a separate collection, then import `room_reference_raw.glb` as the detailed reference. The clean mesh is intended for alignment and proportions; the raw mesh is intended for furniture, material, and seam reference. The GLBs use metres, floor-aligned Blender Z-up coordinates, and embedded JPEG atlases.
