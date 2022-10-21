import os

import gin
import pytorch_lightning as pl
import torch
from effortless_config import Config
from torch.utils.data import DataLoader

import rave
import rave.core

if __name__ == "__main__":

    class args(Config):
        GIN = "configs/rave_v2.gin"

        WAV = None
        PREPROCESSED = None
        MAX_STEPS = 6000000
        VAL_EVERY = 10000
        N_SIGNAL = 131072
        N_CHANNELS = 1

        BATCH = 8
        CKPT = None

        NAME = None

    args.parse_args()

    assert args.NAME is not None, "You must enter a name for this run"
    # assert args.DATASET_PATH is not None, "You must enter a dataset path"

    gin_config = gin.parse_config_file(args.GIN)

    os.makedirs(os.path.join("runs", args.NAME, "rave"), exist_ok=True)

    rave.core.copy_config(
        gin_config.filename,
        os.path.join("runs", args.NAME, "rave", "config.gin"),
    )

    model = rave.RAVE(n_channels=args.N_CHANNELS)

    dataset = rave.core.get_dataset(
        args.WAV,
        args.PREPROCESSED,
        model.sr,
        args.N_SIGNAL,
        args.N_CHANNELS,
    )
    train, val = rave.core.split_dataset(dataset, 98)
    train = DataLoader(train, args.BATCH, True, drop_last=True, num_workers=8)
    val = DataLoader(val, args.BATCH, False, num_workers=8)

    # CHECKPOINT CALLBACKS
    validation_checkpoint = pl.callbacks.ModelCheckpoint(monitor="validation",
                                                         filename="best")
    last_checkpoint = pl.callbacks.ModelCheckpoint(filename="last")

    val_check = {}
    if len(train) >= args.VAL_EVERY:
        val_check["val_check_interval"] = args.VAL_EVERY
    else:
        nepoch = args.VAL_EVERY // len(train)
        val_check["check_val_every_n_epoch"] = nepoch

    trainer = pl.Trainer(
        logger=pl.loggers.TensorBoardLogger(
            os.path.join("runs", args.NAME),
            name="rave",
        ),
        gpus=rave.core.setup_gpu(),
        callbacks=[validation_checkpoint, last_checkpoint],
        max_epochs=100000,
        max_steps=args.MAX_STEPS,
        profiler="simple",
        **val_check,
    )

    run = rave.core.search_for_run(args.CKPT)
    if run is not None:
        loaded_run = torch.load(run, map_location='cpu')
        trainer.fit_loop.epoch_loop._batches_that_stepped = loaded_run['global_step']
        try:
            model = model.load_state_dict(loaded_run['state_dict'], strict=False)
        except RuntimeError:
            pass

    trainer.fit(model, train, val)#, ckpt_path=run)
