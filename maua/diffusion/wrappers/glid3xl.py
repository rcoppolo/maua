import os
import sys
from functools import partial

import clip
import torch
from tqdm import trange

from ...utility import download
from ..conditioning import TextPrompt
from .base import DiffusionWrapper

sys.path.append(os.path.abspath(os.path.dirname(__file__)) + "/../../submodules/GLID3XL/")
from encoders.modules import BERTEmbedder
from guided_diffusion.script_util import create_model_and_diffusion, model_and_diffusion_defaults

MODEL_URLS = {
    "glid3xl-bert": "https://dall-3.com/models/glid-3-xl/bert.pt",
    "glid3xl-kl-f8": "https://dall-3.com/models/glid-3-xl/kl-f8.pt",
    "glid3xl-diffusion": "https://dall-3.com/models/glid-3-xl/diffusion.pt",
    "glid3xl-finetune": "https://dall-3.com/models/glid-3-xl/finetune.pt",
    "glid3xl-inpaint": "https://dall-3.com/models/glid-3-xl/inpaint.pt",
}


def create_models(
    checkpoint="finetune",
    timestep_respacing="27",
    device=torch.device("cuda" if torch.cuda.is_available() else "cpu"),
    use_backward_guidance=False,
    diffusion_steps=1000,
):
    checkpoint_path = f"modelzoo/glid3xl-{checkpoint}.pt"
    url = MODEL_URLS[f"glid3xl-{checkpoint}"]
    if not os.path.exists(checkpoint_path):
        download(url, checkpoint_path)
    model_state_dict = torch.load(checkpoint_path, map_location="cpu")

    model_params = {
        "attention_resolutions": "32,16,8",
        "class_cond": False,
        "diffusion_steps": diffusion_steps,
        "rescale_timesteps": True,
        "timestep_respacing": timestep_respacing,
        "image_size": 32,
        "learn_sigma": False,
        "noise_schedule": "linear",
        "num_channels": 320,
        "num_heads": 8,
        "num_res_blocks": 2,
        "resblock_updown": False,
        "use_fp16": False,
        "use_scale_shift_norm": False,
        "clip_embed_dim": 768 if "clip_proj.weight" in model_state_dict else None,
        "image_condition": True if model_state_dict["input_blocks.0.0.weight"].shape[1] == 8 else False,
        "super_res_condition": True if "external_block.0.0.weight" in model_state_dict else False,
    }
    print(f'clip_embed_dim={"clip_proj.weight" in model_state_dict}')
    print(f'image_condition={model_state_dict["input_blocks.0.0.weight"].shape[1] == 8}')
    print(f'super_res_condition={"external_block.0.0.weight" in model_state_dict}')

    model_config = model_and_diffusion_defaults()
    model_config.update(model_params)
    model_config["use_fp16"] = device.type == "cuda"

    # Load models
    model, diffusion = create_model_and_diffusion(**model_config)
    model.load_state_dict(model_state_dict, strict=False)
    model.requires_grad_(use_backward_guidance).eval().to(device)
    model.clip_conditioned = model_params["clip_embed_dim"] is not None
    model.image_conditioned = model_params["image_condition"]
    model.super_res_conditioned = model_params["super_res_condition"]

    if model_config["use_fp16"]:
        model.convert_to_fp16()
    else:
        model.convert_to_fp32()

    def set_requires_grad(model, value):
        for param in model.parameters():
            param.requires_grad = value

    # vae
    kl_path = f"modelzoo/glid3xl-kl-f8.pt"
    if not os.path.exists(kl_path):
        download(MODEL_URLS[f"glid3xl-kl-f8"], kl_path)
    ldm = torch.load(kl_path, map_location="cpu")
    ldm.to(device)
    ldm.eval()
    ldm.requires_grad_(use_backward_guidance)
    set_requires_grad(ldm, use_backward_guidance)

    bert_path = f"modelzoo/glid3xl-bert.pt"
    if not os.path.exists(bert_path):
        download(MODEL_URLS[f"glid3xl-bert"], bert_path)
    bert = BERTEmbedder(1280, 32)
    sd = torch.load(bert_path, map_location="cpu")
    bert.load_state_dict(sd)

    bert.to(device)
    bert.half().eval()
    set_requires_grad(bert, False)

    return model, diffusion, ldm, bert


class LatentGradientGuidedConditioning(torch.nn.Module):
    def __init__(self, diffusion, model, bert, ldm, grad_modules, device):
        super().__init__()
        self.diffusion, self.model, self.bert, self.ldm = diffusion, model, bert, ldm
        self.grad_modules = torch.nn.ModuleList(grad_modules)
        self.timestep_map = diffusion.timestep_map
        sqrt_one_minus_alphas_cumprod = torch.from_numpy(diffusion.sqrt_one_minus_alphas_cumprod).float()
        self.register_buffer("sqrt_one_minus_alphas_cumprod", sqrt_one_minus_alphas_cumprod)

    def set_targets(self, prompts, noise):
        for grad_module in self.grad_modules:
            grad_module.set_targets(prompts)

    def forward(self, x, t, kw={}):
        ot = t.clone()
        t = torch.tensor([self.timestep_map.index(t) for t in t.long()], device=x.device, dtype=torch.long)

        with torch.enable_grad():
            x = x[: x.shape[0] // 2].detach().requires_grad_()

            out = self.diffusion.p_mean_variance(self.model, x, t, clip_denoised=False, model_kwargs=kw)["pred_xstart"]
            sigma = self.sqrt_one_minus_alphas_cumprod[t].reshape(-1, 1, 1, 1)
            out = out * sigma + x * (1 - sigma)
            img = self.ldm.decode(out / 0.18215)  # TODO where does 0.18215 come from?????

            img_grad = torch.zeros_like(img)
            for grad_mod in self.grad_modules:
                sub_grad = grad_mod(img, ot)

                if torch.isnan(sub_grad).any():
                    print(grad_mod.__class__.__name__, "NaN")
                    sub_grad = torch.zeros_like(img)

                img_grad += sub_grad

            grad = -torch.autograd.grad(img, x, img_grad)[0]

        return grad


class GLID3XL(DiffusionWrapper):
    def __init__(
        self,
        grad_modules=[],
        sampler="ddim",
        timesteps=100,
        model_checkpoint="finetuned",
        device=torch.device("cuda" if torch.cuda.is_available() else "cpu"),
        ddim_eta=0,
    ):
        super().__init__()
        self.use_backward_guidance = any(gm.scale > 0 for gm in grad_modules)
        self.model, self.diffusion, self.ldm, self.bert = create_models(
            checkpoint=model_checkpoint,
            timestep_respacing=f"ddim{timesteps}" if sampler == "ddim" else str(timesteps),
            use_backward_guidance=self.use_backward_guidance,
            device=device,
        )
        if self.model.clip_conditioned:
            self.clip_model, _ = clip.load("ViT-L/14", device=device, jit=False)
            self.clip_model.eval().requires_grad_(False)
        self.conditioning = (
            LatentGradientGuidedConditioning(
                self.diffusion, self.model, self.bert, self.ldm, [gm for gm in grad_modules if gm.scale != 0], device
            ).to(device)
            if self.use_backward_guidance
            else None
        )

        def model_fn(x_t, ts, scale, **kwargs):
            half = x_t[: len(x_t) // 2]
            combined = torch.cat([half, half], dim=0)
            model_out = self.model(combined, ts, **kwargs)
            eps, rest = model_out[:, :3], model_out[:, 3:]
            cond_eps, uncond_eps = torch.split(eps, len(eps) // 2, dim=0)
            half_eps = uncond_eps + scale * (cond_eps - uncond_eps)
            eps = torch.cat([half_eps, half_eps], dim=0)
            return torch.cat([eps, rest], dim=1)

        if sampler == "p":
            self.sample_fn = lambda _, scale: partial(
                self.diffusion.p_sample, model=partial(model_fn, scale=scale), clip_denoised=False
            )
        elif sampler == "ddim":
            self.sample_fn = lambda _, scale: partial(
                self.diffusion.ddim_sample, model=partial(model_fn, scale=scale), eta=ddim_eta, clip_denoised=False
            )
        elif sampler == "plms":
            self.sample_fn = lambda old_eps, scale: partial(
                (
                    self.diffusion.prk_sample
                    if len(old_eps) < 3
                    else partial(self.diffusion.plms_sample, old_eps=old_eps)
                ),
                model=partial(model_fn, scale=scale),
                clip_denoised=False,
            )
        else:
            raise NotImplementedError()

        self.device = device
        self.model = self.model.to(device)
        self.original_num_steps = self.diffusion.original_num_steps
        self.timestep_map = self.diffusion.timestep_map

    @torch.no_grad()
    def sample(self, img, prompts, start_step, n_steps=None, verbose=True):
        if n_steps is None:
            n_steps = start_step
        t = torch.tensor([start_step] * img.shape[0], device=self.device, dtype=torch.long)

        noise = torch.randn_like(img)
        if self.use_backward_guidance:
            self.conditioning.set_targets([p.to(img) for p in prompts], noise)
        img = self.diffusion.q_sample(img, t, noise)

        for prompt in prompts:
            if isinstance(prompt, TextPrompt):
                text, guidance_scale = prompt()
                break

        negative = ""
        text_emb = self.bert.encode([text] * img.shape[0])
        text_blank = self.bert.encode([negative] * img.shape[0])
        context = torch.cat([text_emb, text_blank], dim=0).float().to(self.device)
        if self.model.clip_conditioned:
            text_emb_clip = self.clip_model.encode_text(clip.tokenize([text] * img.shape[0], truncate=True))
            text_emb_clip_blank = self.clip_model.encode_text(clip.tokenize([negative] * img.shape[0], truncate=True))
            clip_context = torch.cat([text_emb_clip, text_emb_clip_blank], dim=0).float().to(self.device)

        kw = {
            "context": context,
            "clip_embed": clip_context if self.model.clip_conditioned else None,
            "image_embed": None,  # TODO implement inpainting
        }

        t = torch.tensor([start_step] * img.shape[0], device=self.device, dtype=torch.long)

        old_eps = []
        for _ in (trange if verbose else range)(n_steps):
            out = self.sample_fn(old_eps, guidance_scale)(out["sample"], t, cond_fn=self.conditioning, model_kwargs=kw)

            if "eps" in out:  # PLMS bookkeeping
                if len(old_eps) >= 3:
                    old_eps.pop(0)
                old_eps.append(out["eps"])

            t -= 1

        return out["pred_xstart"]

    def forward(self, shape, prompts, model_kwargs={}):
        img = torch.randn(*shape, device=self.device)
        steps = len(self.diffusion.use_timesteps)
        return self.sample(img, prompts, start_step=steps, n_steps=steps, model_kwargs=model_kwargs)
