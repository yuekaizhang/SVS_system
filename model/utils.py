#!/usr/bin/env python3

# Copyright 2020 The Johns Hopkins University (author: Jiatong Shi)


import torch
import numpy as np
import copy
import time
import librosa
import matplotlib.pyplot as plt
from librosa.output import write_wav
from librosa.display import specshow
from scipy import signal

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def create_src_key_padding_mask(src_len, max_len):
    bs = len(src_len)
    mask = np.zeros((bs, max_len))
    for i in range(bs):
        mask[i, :src_len[i]] = 1
    return torch.from_numpy(mask).float()


def train_one_epoch(train_loader, model, device, optimizer, criterion, perceptual_entropy, args):
    losses = AverageMeter()
    if args.perceptual_loss > 0:
        pe_losses = AverageMeter()
    model.train()

    log_save_dir = os.path.join(args.model_save_dir, "log_train_figure")
    if not os.path.exists(log_save_dir):
        os.makedirs(log_save_dir)

    start = time.time()
    for step, (phone, beat, pitch, spec, real, imag, length, chars, char_len_list) in enumerate(train_loader, 1):
        phone = phone.to(device)
        beat = beat.to(device)
        pitch = pitch.to(device).float()
        spec = spec.to(device).float()
        real = real.to(device).float()
        imag = imag.to(device).float()
        chars = chars.to(device)
        length_mask = create_src_key_padding_mask(length, args.num_frames)
        length_mask = length_mask.unsqueeze(2)
        length_mask = length_mask.repeat(1, 1, spec.shape[2]).float()
        length_mask = length_mask.to(device)
        length = length.to(device)
        char_len_list = char_len_list.to(device)

        output, att = model(chars, phone, pitch, beat, src_key_padding_mask=length,
                       char_key_padding_mask=char_len_list)

        train_loss = criterion(output, spec, length_mask)
        if args.perceptual_loss > 0:
            pe_loss = perceptual_entropy(output, real, imag)
            final_loss = args.perceptual_loss * pe_loss + (1 - args.perceptual_loss) * train_loss
        else:
            final_loss = train_loss

        optimizer.zero_grad()
        final_loss.backward()
        if args.gradclip > 0:
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.gradclip)
        optimizer.step_and_update_lr()
        losses.update(train_loss.item(), phone.size(0))
        if args.perceptual_loss > 0:
            pe_losses.update(pe_loss.item(), phone.size(0))
        if step % 100 == 0:
            end = time.time()
            log_figure(step, output, spec, att, length, log_save_dir, args)
            if args.perceptual_loss > 0:
                print("step {}: train_loss {}; pe_loss {}-- sum_time: {}s".format(step, losses.avg, pe_losses.avg, end - start))
            else:
                print("step {}: train_loss {} -- sum_time: {}s".format(step, losses.avg, end - start))

    if args.perceptual_loss > 0:
        info = {'loss': losses.avg, 'pe_loss': pe_losses.avg}
    else:
        info = {'loss': losses.avg}
    return info


def validate(dev_loader, model, device, criterion, perceptual_entropy, args):
    losses = AverageMeter()
    if args.perceptual_loss > 0:
        pe_losses = AverageMeter()
    model.eval()

    log_save_dir = os.path.join(args.model_save_dir, "log_val_figure")
    if not os.path.exists(log_save_dir):
        os.makedirs(log_save_dir)

    with torch.no_grad():
        for step, (phone, beat, pitch, spec, real, imag, length, chars, char_len_list) in enumerate(dev_loader, 1):
            phone = phone.to(device).to(torch.int64)
            beat = beat.to(device).to(torch.int64)
            pitch = pitch.to(device).float()
            spec = spec.to(device).float()
            real = real.to(device).float()
            imag = imag.to(device).float()
            chars = chars.to(device)
            length = length.to(device)
            length_mask = create_src_key_padding_mask(length, args.num_frames)
            length_mask = length_mask.unsqueeze(2)
            length_mask = length_mask.repeat(1, 1, spec.shape[2]).float()
            length_mask = length_mask.to(device)
            char_len_list = char_len_list.to(device)

            output, att = model(chars, phone, pitch, beat, src_key_padding_mask=length,
                           char_key_padding_mask=char_len_list)

            dev_loss = criterion(output, spec, length_mask)
            losses.update(dev_loss.item(), phone.size(0))
            if args.perceptual_loss > 0:
                pe_loss = perceptual_entropy(output, real, imag)
                pe_losses.update(pe_loss.item(), phone.size(0))
            if step % 10 == 0:
                log_figure(step, output, spec, att, length, log_save_dir, args)
                if args.perceptual_loss > 0:
                    print("step {}: loss {} ; pe_loss {}".format(step, losses.avg, pe_losses.avg))
                else:
                    print("step {}: loss {}".format(step, losses.avg))
    if args.perceptual_loss > 0:
        info = {'loss': losses.avg, 'pe_loss': pe_losses.avg}
    else:
        info = {'loss': losses.avg}
    return info


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def save_checkpoint(state, model_filename):
    torch.save(state, model_filename)
    return 0


def record_info(train_info, dev_info, epoch, logger):
    loss_info = {
        "train_loss": train_info['loss'],
        "dev_loss": dev_info['loss']}
    logger.add_scalars("losses", loss_info, epoch)
    return 0


def invert_spectrogram(spectrogram, win_length, hop_length):
    '''Applies inverse fft.
    Args:
      spectrogram: [1+n_fft//2, t]
    '''
    return librosa.istft(spectrogram, hop_length, win_length=win_length, window="hann")


def griffin_lim(spectrogram, iter_vocoder, n_fft, hop_length, win_length):
    '''Applies Griffin-Lim's raw.'''
    X_best = copy.deepcopy(spectrogram)
    for i in range(iter_vocoder):
        X_t = invert_spectrogram(X_best, win_length, hop_length)
        est = librosa.stft(X_t, n_fft, hop_length, win_length=win_length)
        phase = est / np.maximum(1e-8, np.abs(est))
        X_best = spectrogram * phase
    X_t = invert_spectrogram(X_best, win_length, hop_length)
    y = np.real(X_t)
    return y


def spectrogram2wav(mag, max_db, ref_db, preemphasis, power, sr, hop_length, win_length):
    '''# Generate wave file from linear magnitude spectrogram
    Args:
      mag: A numpy array of (T, 1+n_fft//2)
    Returns:
      wav: A 1-D numpy array.
    '''
    hop_length = int(hop_length * sr)
    win_length = int(win_length * sr)
    n_fft = win_length

    # transpose
    mag = mag.T

    # de-noramlize
    mag = (np.clip(mag, 0, 1) * max_db) - max_db + ref_db

    # to amplitude
    mag = np.power(10.0, mag * 0.05)

    # wav reconstruction
    wav = griffin_lim(mag** power, 100, n_fft, hop_length, win_length)

    # de-preemphasis
    wav = signal.lfilter([1], [1, -preemphasis], wav)

    # trim
    wav, _ = librosa.effects.trim(wav)

    return wav.astype(np.float32)


def log_figure(step, output, spec, att, length, save_dir, args):
    # only get one sample from a batch
    # save wav and plot spectrogram
    output = output.cpu().detach().numpy()[0]
    out_spec = spec.cpu().detach().numpy()[0]
    length = length.cpu().detach().numpy()[0]
    att = att.cpu().detach().numpy()[0]
    output = output[:length]
    out_spec = out_spec[:length]
    att = att[:, :length, :length]
    wav = spectrogram2wav(output, args.max_db, args.ref_db, args.preemphasis, args.power, args.sampling_rate, args.frame_shift, args.frame_length)
    wav_true = spectrogram2wav(out_spec, args.max_db, args.ref_db, args.preemphasis, args.power, args.sampling_rate, args.frame_shift, args.frame_length)
    write_wav(os.path.join(save_dir, '{}.wav'.format(step)), wav, args.sampling_rate)
    write_wav(os.path.join(save_dir, '{}_true.wav'.format(step)), wav_true, args.sampling_rate)
    plt.subplot(1, 2, 1)
    specshow(output.T)
    plt.title("prediction")
    plt.subplot(1, 2, 2)
    specshow(out_spec.T)
    plt.title("ground_truth")
    plt.savefig(os.path.join(save_dir, '{}.png'.format(step)))
    plt.subplot(1, 4, 1)
    specshow(att[0])
    plt.subplot(1, 4, 2)
    specshow(att[1])
    plt.subplot(1, 4, 3)
    specshow(att[2])
    plt.subplot(1, 4, 4)
    specshow(att[3])
    plt.savefig(os.path.join(save_dir, '{}_att.png'.format(step)))