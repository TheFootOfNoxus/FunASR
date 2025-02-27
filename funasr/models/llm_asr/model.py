import logging
from typing import Union, Dict, List, Tuple, Optional

import time
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.cuda.amp import autocast

from funasr.models.scama.utils import sequence_mask
from funasr.losses.label_smoothing_loss import LabelSmoothingLoss
from funasr.models.ctc.ctc import CTC
from funasr.models.transformer.utils.add_sos_eos import add_sos_eos
from funasr.metrics.compute_acc import th_accuracy, compute_accuracy
# from funasr.models.e2e_asr_common import ErrorCalculator
from funasr.train_utils.device_funcs import force_gatherable
from funasr.utils.load_utils import load_audio_text_image_video, extract_fbank
from funasr.utils import postprocess_utils
from funasr.utils.datadir_writer import DatadirWriter
from funasr.register import tables


@tables.register("model_classes", "LLMASRNAR")
class LLMASRNAR(nn.Module):
    """ """
    
    def __init__(
        self,
        specaug: str = None,
        specaug_conf: dict = None,
        normalize: str = None,
        normalize_conf: dict = None,
        encoder: str = None,
        encoder_conf: dict = None,
        decoder: str = None,
        decoder_conf: dict = None,
        ctc: str = None,
        ctc_conf: dict = None,
        ctc_weight: float = 0.5,
        llm: str = None,
        llm_conf: dict = None,
        adaptor: str = None,
        adaptor_conf: dict = None,
        input_size: int = 80,
        vocab_size: int = -1,
        ignore_id: int = -1,
        blank_id: int = 0,
        sos: int = 1,
        eos: int = 2,
        lsm_weight: float = 0.0,
        length_normalized_loss: bool = False,
        report_cer: bool = True,
        report_wer: bool = True,
        sym_space: str = "<space>",
        sym_blank: str = "<blank>",
        # extract_feats_in_collect_stats: bool = True,
        share_embedding: bool = False,
        # preencoder: Optional[AbsPreEncoder] = None,
        # postencoder: Optional[AbsPostEncoder] = None,
        **kwargs,
    ):
        
        super().__init__()
        
        if specaug is not None:
            specaug_class = tables.specaug_classes.get(specaug)
            specaug = specaug_class(**specaug_conf)
        if normalize is not None:
            normalize_class = tables.normalize_classes.get(normalize)
            normalize = normalize_class(**normalize_conf)
        
        # audio encoder
        hub = encoder_conf.get("hub", None)
        if hub == "funasr":
            from funasr import AutoModel
            init_param_path = encoder_conf.get("init_param_path", "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch")
            model = AutoModel(model=init_param_path, model_revision="v2.0.4")
            # frontend = model.kwargs.get("frontend")
            model.model.decoder = None
            
            self.audio_encoder = model.model
            # self.frontend = frontend
            
        elif hub == "hf":
            pass
        else:
            encoder_class = tables.encoder_classes.get(encoder)
            encoder = encoder_class(input_size=input_size, **encoder_conf)
            encoder_output_size = encoder.output_size()

        # llm
        hub = llm_conf.get("hub", "hf")
        self.llm = None
        if hub == "hf":
            from transformers import AutoModelForCausalLM, AutoTokenizer, AutoConfig

            init_param_path = llm_conf.get("init_param_path", "vicuna-7b-v1.5")
            model = AutoModelForCausalLM.from_pretrained(
                init_param_path,
                load_in_8bit=None,
                device_map=None,
                use_cache=None,
            )
            freeze = llm_conf.get("freeze", True)
            if freeze:
                for name, param in model.named_parameters():
                    param.requires_grad = False
                model.eval()
            self.llm = model
        
        # adaptor
        adaptor_class = tables.adaptor_classes.get(adaptor)
        adaptor = adaptor_class(**adaptor_conf)
        
        self.adaptor = adaptor
        
        
        self.blank_id = blank_id
        self.sos = sos if sos is not None else vocab_size - 1
        self.eos = eos if eos is not None else vocab_size - 1
        self.vocab_size = vocab_size
        self.ignore_id = ignore_id
        self.specaug = specaug
        self.normalize = normalize
        self.encoder = encoder


        self.criterion_att = LabelSmoothingLoss(
            size=vocab_size,
            padding_idx=ignore_id,
            smoothing=lsm_weight,
            normalize_length=length_normalized_loss,
        )
        #
        # if report_cer or report_wer:
        #     self.error_calculator = ErrorCalculator(
        #         token_list, sym_space, sym_blank, report_cer, report_wer
        #     )
        #
        self.error_calculator = None

        self.length_normalized_loss = length_normalized_loss
        self.beam_search = None
    
    def forward(
        self,
        speech: torch.Tensor,
        speech_lengths: torch.Tensor,
        text: torch.Tensor,
        text_lengths: torch.Tensor,
        input_ids: torch.Tensor,
        attention_mask:torch.Tensor,
        labels_ids: torch.Tensor,
        label_mask: torch.Tensor,
        audio_mask: torch.Tensor,
        **kwargs,
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor], torch.Tensor]:
        """Encoder + Decoder + Calc loss
        Args:
                speech: (Batch, Length, ...)
                speech_lengths: (Batch, )
                text: (Batch, Length)
                text_lengths: (Batch,)
        """
        # import pdb;
        # pdb.set_trace()
        if len(text_lengths.size()) > 1:
            text_lengths = text_lengths[:, 0]
        if len(speech_lengths.size()) > 1:
            speech_lengths = speech_lengths[:, 0]
        
        batch_size = speech.shape[0]
        
        # audio encoder
        encoder_out, encoder_out_lens = self.encode(speech, speech_lengths, audio_mask=audio_mask)
        
        # adaptor
        encoder_out = self.adaptor(encoder_out)

        if input_ids is not None:
            input_ids[input_ids == -1] = 0
            input_ids[input_ids == -100] = 0
            if hasattr(self.llm.model, "embed_tokens"):
                inputs_embeds = self.llm.model.embed_tokens(input_ids)
            elif hasattr(self.llm.model.model, "embed_tokens"):
                inputs_embeds = self.llm.model.model.embed_tokens(input_ids)
            else:
                inputs_embeds = self.llm.model.model.model.embed_tokens(input_ids)

            if audio_mask is not None:
                batch_size, token_num, dims = inputs_embeds.shape
                _, l, _ = encoder_out.shape
                encoder_outs_pad = F.pad(encoder_out, (0, 0, token_num-l-1, 1, 0, 0), value=0.0)
                inputs_embeds = encoder_outs_pad * audio_mask[:, :, None] + inputs_embeds * (1.0-audio_mask[:, :, None])
                inputs_embeds = F.pad(inputs_embeds[:, 1:, :], (0, 0, 0, 1, 0, 0), value=0.0)

        model_outputs = self.llm(inputs_embeds=inputs_embeds, attention_mask=attention_mask, labels=labels_ids)
        loss = model_outputs.loss


        stats = {}
        with torch.no_grad():
            preds = torch.argmax(model_outputs.logits, -1)
            acc_att = compute_accuracy(preds[:, :-1], labels_ids[:, 1:], ignore_label=-100)
            stats["acc"] = acc_att

        stats["loss"] = torch.clone(loss.detach())

        # force_gatherable: to-device and to-tensor if scalar for DataParallel
        if self.length_normalized_loss:
            batch_size = int((text_lengths + 1).sum())
        loss, stats, weight = force_gatherable((loss, stats, batch_size), loss.device)
        return loss, stats, weight
    
    def encode(
        self, speech: torch.Tensor, speech_lengths: torch.Tensor, **kwargs,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
    
        audio_mask = kwargs.get("audio_mask", None)
        audio_token_lengths = audio_mask.sum(-1) if audio_mask is not None else None

        batch = {"speech": speech, "speech_lengths": speech_lengths}
        enc, enc_lens = self.audio_encoder.encode(**batch)
        with autocast(False):
            enc_mask = sequence_mask(enc_lens, enc.size(1), device=enc.device)[:, None, :]
            pre_acoustic_embeds, pre_token_length, _, _ = self.audio_encoder.predictor(enc,
                                                                               mask=enc_mask,
                                                                               target_label_length=audio_token_lengths,
                                                                               )

        return pre_acoustic_embeds, pre_token_length


    def inference(self,
                  data_in,
                  data_lengths=None,
                  key: list = None,
                  tokenizer=None,
                  frontend=None,
                  **kwargs,
                  ):
        
        prompt = kwargs.get("prompt", "Transcribe speech to text.")
        
        if kwargs.get("batch_size", 1) > 1:
            raise NotImplementedError("batch decoding is not implemented")


        
        meta_data = {}
        if isinstance(data_in, torch.Tensor) and kwargs.get("data_type", "sound") == "fbank":  # fbank
            speech, speech_lengths = data_in, data_lengths
            if len(speech.shape) < 3:
                speech = speech[None, :, :]
            if speech_lengths is None:
                speech_lengths = speech.shape[1]
        else:
            # extract fbank feats
            time1 = time.perf_counter()
            audio_sample_list = load_audio_text_image_video(data_in, fs=frontend.fs, audio_fs=kwargs.get("fs", 16000),
                                                            data_type=kwargs.get("data_type", "sound"),
                                                            tokenizer=tokenizer)
            time2 = time.perf_counter()
            meta_data["load_data"] = f"{time2 - time1:0.3f}"
            speech, speech_lengths = extract_fbank(audio_sample_list, data_type=kwargs.get("data_type", "sound"),
                                                   frontend=frontend)
            time3 = time.perf_counter()
            meta_data["extract_feat"] = f"{time3 - time2:0.3f}"
            meta_data["batch_data_time"] = speech_lengths.sum().item() * frontend.frame_shift * frontend.lfr_n / 1000
        
        speech = speech.to(device=kwargs["device"])
        speech_lengths = speech_lengths.to(device=kwargs["device"])
        
        # Encoder
        encoder_out, encoder_out_lens = self.encode(speech, speech_lengths)

        # adaptor
        encoder_out = self.adaptor(encoder_out)
        
    
        prompt_pre = "USER: \nINSTRUCTION: {}\nINPUT: ".format(prompt)
        prompt_ids = tokenizer.encode(prompt_pre)
        prompt_length = len(prompt_ids)
        prompt_ids = torch.tensor(prompt_ids, dtype=torch.int64).to(kwargs["device"])


        if hasattr(self.llm.model, "embed_tokens"):
            inputs_embeds = self.llm.model.embed_tokens(prompt_ids)
        elif hasattr(self.llm.model.model, "embed_tokens"):
            inputs_embeds = self.llm.model.model.embed_tokens(prompt_ids)
        else:
            inputs_embeds = self.llm.model.model.model.embed_tokens(prompt_ids)

        inputs_embeds = torch.cat((inputs_embeds[None, :, :], encoder_out), dim=1)  # [prompt, audio]
        attention_mask = torch.ones(inputs_embeds.size()[:-1], dtype=torch.long).to(kwargs["device"])
        
        model_outputs = self.llm.generate(
            inputs_embeds=inputs_embeds,
            max_length=kwargs.get("max_length", 200),
            max_new_tokens=kwargs.get("max_new_tokens", 200),
            num_beams=kwargs.get("num_beams", 4),
            do_sample=kwargs.get("do_sample", False),
            min_length=kwargs.get("min_length", 1),
            top_p=kwargs.get("top_p", 1.0),
            repetition_penalty=kwargs.get("repetition_penalty", 1.0),
            length_penalty=kwargs.get("length_penalty", 1.0),
            temperature=kwargs.get("temperature", 1.0),
            attention_mask=attention_mask,
            bos_token_id=tokenizer.bos_token_id,
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.pad_token_id
        )

        text = tokenizer.batch_decode(model_outputs, add_special_tokens=False, skip_special_tokens=True)
        
        ibest_writer = None
        if kwargs.get("output_dir") is not None:
            if not hasattr(self, "writer"):
                self.writer = DatadirWriter(kwargs.get("output_dir"))
            ibest_writer = self.writer[f"{0 + 1}best_recog"]

        results = []
        result_i = {"key": key[0], "text": text}
        results.append(result_i)

        if ibest_writer is not None:
            ibest_writer["text"][key[0]] = text
        
        
        
        
        return results, meta_data

