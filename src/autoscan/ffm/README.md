# FlakeFinder

A desktop app for detecting and visualizing 2D-material flakes (graphene, hBN)
in optical microscope images. Two operating modes:

**Detection mode** — runs the trained GMM detector (vendored from
   [2DMatGMM](https://github.com/Jaluus/2DMatGMM), MIT) on any image.
   Reliable for graphene on Si/90 nm SiO2; needs per-substrate training
   for other combinations.


## Training new weights

Organise data like this:

```
my_dataset/
├── train_images/img001.png ...
└── train_semantic_masks/img001.png ...   # 8-bit greyscale, pixel value = layer count
```

Run:

```bash
python -m flakefinder.train_substrate \
    --material Graphene --substrate SiO2_285nm \
    --data-dir ./my_dataset --num-layers 4
```

Output: `flakefinder/user_weights/Graphene_SiO2_285nm_GMM.json`.

## Project layout

```
flakefinder/
├── run_gui.py
├── requirements.txt
├── README.md
└── flakefinder/
    ├── __init__.py
    ├── core.py                # Detector wrapper + result dataclasses
    ├── materials.py           # material/substrate registry
    ├── overlay.py             # annotated image rendering (OpenCV)
    ├── gui.py                 # Tkinter app
    ├── demo_mode.py           # pHash-based demo lookup
    ├── demo_assets/           # demo images + labelme JSONs
    ├── train_substrate.py     # training CLI
    ├── user_weights/          # user-trained GMMs land here
    └── vendor/                # 2DMatGMM (MIT, vendored)
```

## Limitations

- **Discrete layer assumption.** For hBN >~ 10 nm, contrast becomes
  quasi-continuous; treat reported layer count as a bin index.
- **First detection call is slow.** Numba JIT-compiles on first call
  (~5 s); subsequent calls are ~100-200 ms per 2 MP image.
- **The nm thickness is computed**, not measured. For absolute thickness,
  use AFM.

## Citation

```bibtex
@article{Uslu2024,
  author  = {Uslu, Jan-Lucas and Ouaj, Taoufiq and Tebbe, David and others},
  title   = {An open-source robust machine learning platform for real-time
             detection and classification of 2D material flakes},
  journal = {Machine Learning: Science and Technology},
  volume  = {5}, number = {1}, pages = {015027}, year = {2024},
  doi     = {10.1088/2632-2153/ad2287}
}
```
