import os
import numpy as np
import math
import sys
from typing import Iterable, Optional
import torch
from util_tools.mixup import Mixup
from timm.utils import accuracy, ModelEma
import util_tools.utils as utils
from scipy.special import softmax
from einops import rearrange, repeat
import random
import pandas as pd

def composition_train_class_batch(model, samples, target_noun, target_verb, criterion,
                                  nounlist, verblist, actionlist, device):
    featnorm = 1
    randContrast = False
    
    if randContrast:
        nounContrast = len(nounlist) 
        verbContrast = 97
         
        # sample positive and negative
        target_noun = [nounlist[i] for i in target_noun]
        target_verb = [verblist[i] for i in target_verb]
        noun_uniqname = np.unique(target_noun)
        verb_uniqname = np.unique(target_verb)
        noun_numNeg = nounContrast - len(noun_uniqname)
        verb_numNeg = verbContrast - len(verb_uniqname)
        noun_complement = list(set(nounlist) - set(noun_uniqname))
        verb_complement = list(set(verblist) - set(verb_uniqname))
        inp_nounlist = noun_uniqname.tolist() + random.sample(noun_complement, min(noun_numNeg, len(noun_complement)))
        inp_verblist = verb_uniqname.tolist() + random.sample(verb_complement, min(verb_numNeg, len(verb_complement)))
        noun_targets = torch.tensor([inp_nounlist.index(n) for n in target_noun]).to(device)
        verb_targets = torch.tensor([inp_verblist.index(v) for v in target_verb]).to(device)
    else:
        noun_targets, verb_targets = target_noun, target_verb
    
    outputs_noun, outputs_verb, nounFeature, verbFeature = model(samples, nounlist, verblist)
    if featnorm:
        outputs_noun = outputs_noun / outputs_noun.norm(dim=-1, keepdim=True)
        outputs_verb = outputs_verb / outputs_verb.norm(dim=-1, keepdim=True)
        nounFeature = nounFeature / nounFeature.norm(dim=-1, keepdim=True)
        verbFeature = verbFeature / verbFeature.norm(dim=-1, keepdim=True)
        noun_logits = outputs_noun @ nounFeature.t() / 0.07 if nounFeature.dim() == 2 else torch.bmm(nounFeature, outputs_noun.unsqueeze(-1)).squeeze(-1) / 0.07
        verb_logits = outputs_verb @ verbFeature.t() / 0.07 if verbFeature.dim() == 2 else torch.bmm(verbFeature, outputs_verb.unsqueeze(-1)).squeeze(-1) / 0.07
    else:
        noun_logits = outputs_noun @ nounFeature.t() if nounFeature.dim() == 2 else torch.bmm(nounFeature, outputs_noun.unsqueeze(-1)).squeeze(-1)
        verb_logits = outputs_verb @ verbFeature.t() if verbFeature.dim() == 2 else torch.bmm(verbFeature, outputs_verb.unsqueeze(-1)).squeeze(-1)
        
    loss_noun = criterion(noun_logits, noun_targets)
    loss_verb = criterion(verb_logits, verb_targets)
    total_loss = loss_noun + loss_verb
    return total_loss, loss_noun, loss_verb, noun_logits, verb_logits



def get_loss_scale_for_deepspeed(model):
    optimizer = model.optimizer
    return optimizer.loss_scale if hasattr(optimizer, "loss_scale") else optimizer.cur_scale

def train_one_epoch(args, model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, loss_scaler, max_norm: float = 0,
                    model_ema: Optional[ModelEma] = None, mixup_fn: Optional[Mixup] = None, log_writer=None,
                    start_steps=None, lr_schedule_values=None, wd_schedule_values=None,
                    num_training_steps_per_epoch=None, update_freq=None, class_list=None):
    model.train(True)
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    metric_logger.add_meter('min_lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 20
    
    # prompt setting
    nounlist, noundict, nountoken, verblist, verbdict, verbtoken, actionlist, actiondict, actiontoken, seen_nounlist, seen_noundict = class_list
        
    if loss_scaler is None:
        model.zero_grad()
        model.micro_steps = 0
    else:
        optimizer.zero_grad()
    for data_iter_step, (samples, targets, target_id, _, _, _) in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
        step = data_iter_step // update_freq
        if step >= num_training_steps_per_epoch:
            continue
        it = start_steps + step  # global training iteration
        # Update LR & WD for the first acc
        if lr_schedule_values is not None or wd_schedule_values is not None and data_iter_step % update_freq == 0:
            for i, param_group in enumerate(optimizer.param_groups):
                if lr_schedule_values is not None:
                    param_group["lr"] = lr_schedule_values[it] * param_group["lr_scale"]
                if wd_schedule_values is not None and param_group["weight_decay"] > 0:
                    param_group["weight_decay"] = wd_schedule_values[it]

        samples = samples.to(device, non_blocking=True)
        targets = targets.to(device, non_blocking=True)
        action_target = (targets[:,1] * 1000) + targets[:,0]
        batch_size = samples.shape[0]
        noun_targets_name = [nounlist[label[0]] for label in targets]
        trimmed_targets = [seen_nounlist.index(name) for name in noun_targets_name]
        trimmed_targets = torch.tensor(trimmed_targets).to(device, non_blocking=True)
        
        if mixup_fn is not None:
            samples, target_noun, target_verb = mixup_fn(samples, targets)
        else:
            target_noun, target_verb = targets[:,0], targets[:,1]
        
        if loss_scaler is None: # deepspeed 라면 이부분이 실행된다. 근데 else문은 업데이트 안되있어서 deepspeed 없으면 오류 발생
            samples = samples.half()
            loss, loss_noun, loss_verb, noun_logits, verb_logits = composition_train_class_batch(
                model, samples, trimmed_targets, target_verb, criterion, seen_nounlist, verblist, actionlist, args.device)
        else:
            with torch.cuda.amp.autocast():
                samples = samples.half()
                loss, outputs_noun, outpus_verb = composition_train_class_batch(
                    model, samples, target_noun, target_verb, criterion)
        loss_value = loss.item()   

        acc1_action, acc5_action = action_accuracy(noun_logits, verb_logits, action_target, topk=(1,5))
        # acc1_action, acc5_action = action_accuracy(outputs_noun, noun_logits, verb_logits, topk=(1,5))
        # acc1_action, acc5_action = action_accuracy(noun_logits, verb_logits, action_target, topk=(1,5))
        top1_noun_acc, top5_noun_acc = accuracy(noun_logits, targets[:,0], topk=(1, 5))
        top1_verb_acc, top5_verb_acc = accuracy(verb_logits, targets[:,1], topk=(1, 5))

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value), force=True)
            sys.exit(1)

        if loss_scaler is None:
            loss /= update_freq
            model.backward(loss)
            model.step()

            if (data_iter_step + 1) % update_freq == 0:
                # model.zero_grad()
                # Deepspeed will call step() & model.zero_grad() automatic
                if model_ema is not None:
                    model_ema.update(model)
            grad_norm = None
            loss_scale_value = get_loss_scale_for_deepspeed(model)
        else:
            # this attribute is added by timm on one optimizer (adahessian)
            is_second_order = hasattr(optimizer, 'is_second_order') and optimizer.is_second_order
            loss /= update_freq
            grad_norm = loss_scaler(loss, optimizer, clip_grad=max_norm,
                                    parameters=model.parameters(), create_graph=is_second_order,
                                    update_grad=(data_iter_step + 1) % update_freq == 0)
            if (data_iter_step + 1) % update_freq == 0:
                optimizer.zero_grad()
                if model_ema is not None:
                    model_ema.update(model)
            loss_scale_value = loss_scaler.state_dict()["scale"]

        torch.cuda.synchronize()

        if mixup_fn is None:
            pass
            class_acc = None
            # class_acc = (output.max(-1)[-1] == targets).float().mean()
        else:
            class_acc = None
        metric_logger.update(loss=loss_value)
        metric_logger.update(loss_noun=loss_noun)
        metric_logger.update(loss_verb=loss_verb)
        metric_logger.update(class_acc=class_acc)
        metric_logger.update(loss_scale=loss_scale_value)
        min_lr = 10.
        max_lr = 0.
        for group in optimizer.param_groups:
            min_lr = min(min_lr, group["lr"])
            max_lr = max(max_lr, group["lr"])

        metric_logger.update(lr=max_lr)
        metric_logger.update(min_lr=min_lr)
        weight_decay_value = None
        for group in optimizer.param_groups:
            if group["weight_decay"] > 0:
                weight_decay_value = group["weight_decay"]
        metric_logger.update(weight_decay=weight_decay_value)
        metric_logger.update(grad_norm=grad_norm)
        metric_logger.update(acc1_action=acc1_action.item())
        metric_logger.update(acc1_noun=top1_noun_acc.item())
        metric_logger.update(acc1_verb=top1_verb_acc.item())
        metric_logger.update(acc5_noun=top5_noun_acc.item())
        metric_logger.update(acc5_verb=top5_verb_acc.item())
        metric_logger.meters['acc1_action'].update(acc1_action.item(), n=batch_size)
        metric_logger.meters['acc1_noun'].update(top1_noun_acc.item(), n=batch_size)
        metric_logger.meters['acc1_verb'].update(top1_verb_acc.item(), n=batch_size)
        metric_logger.meters['acc5_noun'].update(top5_noun_acc.item(), n=batch_size)
        metric_logger.meters['acc5_verb'].update(top5_verb_acc.item(), n=batch_size)
        
        if log_writer is not None:
            log_writer.update(loss=loss_value, head="loss")
            log_writer.update(class_acc=class_acc, head="loss")
            log_writer.update(loss_scale=loss_scale_value, head="opt")
            log_writer.update(lr=max_lr, head="opt")
            log_writer.update(min_lr=min_lr, head="opt")
            log_writer.update(weight_decay=weight_decay_value, head="opt")
            log_writer.update(grad_norm=grad_norm, head="opt")

            log_writer.set_step()

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


@torch.no_grad()
def validation_one_epoch(args, data_loader, model, device, class_list):
    criterion = torch.nn.CrossEntropyLoss()

    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Val:'
    
    # prompt setting
    nounlist, noundict, nountoken, verblist, verbdict, verbtoken, actionlist, actiondict, actiontoken, seen_nounlist, seen_noundict = class_list
    featnorm = 1

    # switch to evaluation mode
    model.eval()
    for idx, batch in enumerate(metric_logger.log_every(data_loader, 10, header)):
        samples = batch[0]
        target = batch[1]
        batch_size = samples.shape[0]
        samples = samples.to(device, non_blocking=True)
        target = target.to(device, non_blocking=True)
        action_target = (target[:,1] * 1000) + target[:,0]

        # compute output
        with torch.cuda.amp.autocast():
            if idx == 0 or args.ov == 'both':
                outputs_noun, outputs_verb, nounFeature, verbFeature = model(samples, nounlist, verblist)
            elif args.ov == 'noun':
                outputs_noun, outputs_verb, nounFeature, _ = model(samples, nounlist, verblist[:1])
            elif args.ov == 'verb':
                outputs_noun, outputs_verb, _, verbFeature = model(samples, nounlist[:1], verblist)
            else:
                outputs_noun, outputs_verb, _, _ = model(samples, nounlist[:1], verblist[:1])
                
            if featnorm:
                outputs_noun = outputs_noun / outputs_noun.norm(dim=-1, keepdim=True)
                outputs_verb = outputs_verb / outputs_verb.norm(dim=-1, keepdim=True)
                nounFeature = nounFeature / nounFeature.norm(dim=-1, keepdim=True)
                verbFeature = verbFeature / verbFeature.norm(dim=-1, keepdim=True)
                noun_logits = outputs_noun @ nounFeature.t() / 0.07 if nounFeature.dim() == 2 else torch.bmm(nounFeature, outputs_noun.unsqueeze(-1)).squeeze(-1) / 0.07
                verb_logits = outputs_verb @ verbFeature.t() / 0.07 if verbFeature.dim() == 2 else torch.bmm(verbFeature, outputs_verb.unsqueeze(-1)).squeeze(-1) / 0.07
            else:
                noun_logits = outputs_noun @ nounFeature.t() if nounFeature.dim() == 2 else torch.bmm(nounFeature, outputs_noun.unsqueeze(-1)).squeeze(-1)
                verb_logits = outputs_verb @ verbFeature.t() if verbFeature.dim() == 2 else torch.bmm(verbFeature, outputs_verb.unsqueeze(-1)).squeeze(-1)
                
            loss_noun = criterion(noun_logits, target[:,0])
            loss_verb = criterion(verb_logits, target[:,1])
        
        seen_indicator = batch[1][:,3]    
        seen_logits = noun_logits[seen_indicator == 1]
        unseen_logits = noun_logits[seen_indicator == 0]
        seen_B = seen_logits.size(0)
        unseen_B = unseen_logits.size(0)
        seen_target = target[:,0][seen_indicator == 1]
        unseen_target = target[:,0][seen_indicator == 0]
        seen_action_target = action_target[seen_indicator == 1]
        unseen_action_target = action_target[seen_indicator == 0]
        seen_verb_logits = verb_logits[seen_indicator == 1]
        unseen_verb_logits = verb_logits[seen_indicator == 0]

        acc1_action_seen, acc5_action_seen = (torch.tensor(0), torch.tensor(0)) if seen_B == 0 else action_accuracy(seen_logits, seen_verb_logits, seen_action_target, topk=(1, 5))
        acc1_action_unseen, acc5_action_unseen = (torch.tensor(0), torch.tensor(0)) if unseen_B == 0 else action_accuracy(unseen_logits, unseen_verb_logits, unseen_action_target, topk=(1, 5))
        acc1_action, acc5_action = action_accuracy(noun_logits, verb_logits, action_target, topk=(1, 5))
        acc1_noun_seen, acc5_noun_seen = (torch.tensor(0), torch.tensor(0)) if seen_B == 0 else accuracy(seen_logits, seen_target, topk=(1, 5))
        acc1_noun_unseen, acc5_noun_unseen = (torch.tensor(0), torch.tensor(0)) if unseen_B == 0 else accuracy(unseen_logits, unseen_target, topk=(1, 5))
        acc1_noun, acc5_noun = accuracy(noun_logits, target[:, 0], topk=(1, 5))
        acc1_verb, acc5_verb = accuracy(verb_logits, target[:, 1], topk=(1, 5))

        metric_logger.update(loss_noun=loss_noun.item())
        metric_logger.update(loss_verb=loss_verb.item())
        if seen_B != 0:
            metric_logger.meters['acc1_action_seen'].update(acc1_action_seen.item(), n=seen_B)
            metric_logger.meters['acc1_noun_seen'].update(acc1_noun_seen.item(), n=seen_B)
            metric_logger.meters['acc5_noun_seen'].update(acc5_noun_seen.item(), n=seen_B)
        if unseen_B != 0:
            metric_logger.meters['acc1_action_unseen'].update(acc1_action_unseen.item(), n=unseen_B)
            metric_logger.meters['acc1_noun_unseen'].update(acc1_noun_unseen.item(), n=unseen_B)
            metric_logger.meters['acc5_noun_unseen'].update(acc5_noun_unseen.item(), n=unseen_B)
        metric_logger.meters['acc1_action'].update(acc1_action.item(), n=batch_size)
        metric_logger.meters['acc1_noun'].update(acc1_noun.item(), n=batch_size)
        metric_logger.meters['acc1_verb'].update(acc1_verb.item(), n=batch_size)
        metric_logger.meters['acc5_noun'].update(acc5_noun.item(), n=batch_size)
        metric_logger.meters['acc5_verb'].update(acc5_verb.item(), n=batch_size)
        
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print('* Acc_@1_action {top1_action.global_avg:.3f} Acc_@1_noun {top1_noun.global_avg:.3f} Acc_@1_verb {top1_verb.global_avg:.3f} Acc@5_noun {top5_noun.global_avg:.3f} Acc@5_verb {top5_verb.global_avg:.3f} loss_noun {losses_noun.global_avg:.3f} loss_verb {losses_verb.global_avg:.3f}'
          .format(top1_action=metric_logger.acc1_action, top1_noun=metric_logger.acc1_noun, top1_verb=metric_logger.acc1_verb, top5_noun=metric_logger.acc5_noun, top5_verb=metric_logger.acc5_verb, losses_noun=metric_logger.loss_noun, losses_verb=metric_logger.loss_verb))

    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}



@torch.no_grad()
def final_test(args, data_loader, model, device, file, class_list):
    criterion = torch.nn.CrossEntropyLoss()

    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'
    
    # prompt setting
    nounlist, noundict, nountoken, verblist, verbdict, verbtoken, actionlist, actiondict, actiontoken, seen_nounlist, seen_noundict = class_list
    featnorm = 1

    # switch to evaluation mode
    model.eval()
    final_result = []
    
    for idx, batch in enumerate(metric_logger.log_every(data_loader, 10, header)):
        samples = batch[0]
        target = batch[1]
        ids = batch[2]
        chunk_nb = batch[3]
        split_nb = batch[4]
        batch_size = samples.shape[0]
        samples = samples.to(device, non_blocking=True)
        target = target.to(device, non_blocking=True)
        action_target = (target[:,1] * 1000) + target[:,0]
        
        # compute output
        with torch.cuda.amp.autocast():
            if idx == 0 or args.ov == 'both':
                outputs_noun, outputs_verb, nounFeature, verbFeature = model(samples, nounlist, verblist)
            elif args.ov == 'noun':
                outputs_noun, outputs_verb, nounFeature, _ = model(samples, nounlist, verblist[:1])
            elif args.ov == 'verb':
                outputs_noun, outputs_verb, _, verbFeature = model(samples, nounlist[:1], verblist)
            else:
                outputs_noun, outputs_verb, _, _ = model(samples, nounlist[:1], verblist[:1])
                
            if featnorm:
                outputs_noun = outputs_noun / outputs_noun.norm(dim=-1, keepdim=True)
                outputs_verb = outputs_verb / outputs_verb.norm(dim=-1, keepdim=True)
                nounFeature = nounFeature / nounFeature.norm(dim=-1, keepdim=True)
                verbFeature = verbFeature / verbFeature.norm(dim=-1, keepdim=True)
                noun_logits = outputs_noun @ nounFeature.t() / 0.07 if nounFeature.dim() == 2 else torch.bmm(nounFeature, outputs_noun.unsqueeze(-1)).squeeze(-1) / 0.07
                verb_logits = outputs_verb @ verbFeature.t() / 0.07 if verbFeature.dim() == 2 else torch.bmm(verbFeature, outputs_verb.unsqueeze(-1)).squeeze(-1) / 0.07
            else:
                noun_logits = outputs_noun @ nounFeature.t() if nounFeature.dim() == 2 else torch.bmm(nounFeature, outputs_noun.unsqueeze(-1)).squeeze(-1)
                verb_logits = outputs_verb @ verbFeature.t() if verbFeature.dim() == 2 else torch.bmm(verbFeature, outputs_verb.unsqueeze(-1)).squeeze(-1)
                
            
            # seen_loss_noun = np.zeros(0) if not seen_B else criterion(seen_logits, seen_target)
            # unseen_loss_noun = np.zeros(0) if not unseen_B else criterion(unseen_logits, unseen_target)
            # seen_loss_noun = np.zeros(0) if seen_B else seen_loss_noun
            # unseen_loss_noun = np.zeros(0) if unseen_B else unseen_loss_noun
            loss_noun = criterion(noun_logits, target[:,0])
            loss_verb = criterion(verb_logits, target[:,1])

        
        for i in range(outputs_noun.size(0)):
            string = "{} {} {} {} {} {} {} {}\n".format(ids[i], \
                                                str(noun_logits.data[i].cpu().numpy().tolist()), \
                                                str(verb_logits.data[i].cpu().numpy().tolist()), \
                                                str(int(action_target[i].cpu().numpy())), \
                                                str(int(target[i,0].cpu().numpy())), \
                                                str(int(target[i,1].cpu().numpy())), \
                                                str(int(chunk_nb[i].cpu().numpy())), \
                                                str(int(split_nb[i].cpu().numpy())))
            final_result.append(string)
            
        seen_indicator = batch[1][:,3]    
        seen_logits = noun_logits[seen_indicator == 1]
        unseen_logits = noun_logits[seen_indicator == 0]
        seen_B = seen_logits.size(0)
        unseen_B = unseen_logits.size(0)
        seen_target = target[:,0][seen_indicator == 1]
        unseen_target = target[:,0][seen_indicator == 0]
        seen_action_target = action_target[seen_indicator == 1]
        unseen_action_target = action_target[seen_indicator == 0]
        seen_verb_logits = verb_logits[seen_indicator == 1]
        unseen_verb_logits = verb_logits[seen_indicator == 0]

        acc1_action_seen, acc5_action_seen = (torch.tensor(0), torch.tensor(0)) if seen_B == 0 else action_accuracy(seen_logits, seen_verb_logits, seen_action_target, topk=(1, 5))
        acc1_action_unseen, acc5_action_unseen = (torch.tensor(0), torch.tensor(0)) if unseen_B == 0 else action_accuracy(unseen_logits, unseen_verb_logits, unseen_action_target, topk=(1, 5))
        acc1_action, acc5_action = action_accuracy(noun_logits, verb_logits, action_target, topk=(1, 5))
        acc1_noun_seen, acc5_noun_seen = (torch.tensor(0), torch.tensor(0)) if seen_B == 0 else accuracy(seen_logits, seen_target, topk=(1, 5))
        acc1_noun_unseen, acc5_noun_unseen = (torch.tensor(0), torch.tensor(0)) if unseen_B == 0 else accuracy(unseen_logits, unseen_target, topk=(1, 5))
        acc1_noun, acc5_noun = accuracy(noun_logits, target[:, 0], topk=(1, 5))
        acc1_verb, acc5_verb = accuracy(verb_logits, target[:, 1], topk=(1, 5))

        metric_logger.update(loss_noun=loss_noun.item())
        metric_logger.update(loss_verb=loss_verb.item())
        # metric_logger.update(acc1_action=acc1_action.item())
        # metric_logger.update(acc1_noun=acc1_noun.item())
        # metric_logger.update(acc1_verb=acc1_verb.item())
        # metric_logger.update(acc5_noun=acc5_noun.item())
        # metric_logger.update(acc5_verb=acc5_verb.item())
        if seen_B != 0:
            metric_logger.meters['acc1_action_seen'].update(acc1_action_seen.item(), n=seen_B)
            metric_logger.meters['acc1_noun_seen'].update(acc1_noun_seen.item(), n=seen_B)
            metric_logger.meters['acc5_noun_seen'].update(acc5_noun_seen.item(), n=seen_B)
        if unseen_B != 0:
            metric_logger.meters['acc1_action_unseen'].update(acc1_action_unseen.item(), n=unseen_B)
            metric_logger.meters['acc1_noun_unseen'].update(acc1_noun_unseen.item(), n=unseen_B)
            metric_logger.meters['acc5_noun_unseen'].update(acc5_noun_unseen.item(), n=unseen_B)
        metric_logger.meters['acc1_action'].update(acc1_action.item(), n=batch_size)
        metric_logger.meters['acc1_noun'].update(acc1_noun.item(), n=batch_size)
        metric_logger.meters['acc1_verb'].update(acc1_verb.item(), n=batch_size)
        metric_logger.meters['acc5_noun'].update(acc5_noun.item(), n=batch_size)
        metric_logger.meters['acc5_verb'].update(acc5_verb.item(), n=batch_size)

    if not os.path.exists(file):
        os.mknod(file)
    with open(file, 'w') as f:
        f.write("{}, {}\n".format(acc1_noun, acc5_noun))
        for line in final_result:
            f.write(line)
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print('* Acc_@1_action_seen {top1_action_seen.global_avg:.3f} Acc_@1_action_unseen {top1_action_unseen.global_avg:.3f} Acc_@1_action {top1_action.global_avg:.3f} \
        Acc_@1_noun_seen {top1_noun_seen.global_avg:.3f} Acc_@1_noun_unseen {top1_noun_unseen.global_avg:.3f} Acc_@1_noun {top1_noun.global_avg:.3f} Acc_@1_verb {top1_verb.global_avg:.3f} \
        Acc_@5_noun_seen {top5_noun_seen.global_avg:.3f} Acc_@5_noun_unseen {top5_noun_unseen.global_avg:.3f} Acc@5_noun {top5_noun.global_avg:.3f} Acc@5_verb {top5_verb.global_avg:.3f} \
            loss_noun {losses_noun.global_avg:.3f} loss_verb {losses_verb.global_avg:.3f}'
          .format(top1_action_seen=metric_logger.acc1_action_seen, top1_action_unseen=metric_logger.acc1_action_unseen, top1_action=metric_logger.acc1_action, \
              top1_noun_seen=metric_logger.acc1_noun_seen, top1_noun_unseen=metric_logger.acc1_noun_unseen, top1_noun=metric_logger.acc1_noun, top1_verb=metric_logger.acc1_verb, \
              top5_noun_seen=metric_logger.acc5_noun_seen, top5_noun_unseen=metric_logger.acc5_noun_unseen, top5_noun=metric_logger.acc5_noun, top5_verb=metric_logger.acc5_verb, \
                  losses_noun=metric_logger.loss_noun, losses_verb=metric_logger.loss_verb))

    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


def merge(eval_path, num_tasks, return_result = False):
    dict_feats_noun = {}
    dict_feats_verb = {}
    dict_label = {}
    dict_action_label ={}
    dict_pos = {}
    print("Reading individual output files")

    for x in range(num_tasks):
        file = os.path.join(eval_path, str(x) + '.txt')
        lines = open(file, 'r').readlines()[1:]
        for line in lines:
            line = line.strip()
            name = line.split('[')[0]
            label_action = line.split(']')[2].split(' ')[1]
            label_noun = line.split(']')[2].split(' ')[2]
            label_verb = line.split(']')[2].split(' ')[3]
            chunk_nb = line.split(']')[2].split(' ')[4]
            split_nb = line.split(']')[2].split(' ')[5]
            data_noun = np.fromstring(line.split('[')[1].split(']')[0], dtype=float, sep=',')
            data_verb = np.fromstring(line.split('[')[2].split(']')[0], dtype=float, sep=',')
            data_noun = softmax(data_noun)
            data_verb = softmax(data_verb)
            
            if not name in dict_feats_noun:
                dict_feats_noun[name] = []
                dict_feats_verb[name] = []
                dict_label[name] = 0
                dict_action_label[name] = 0
                dict_pos[name] = []
            if chunk_nb + split_nb in dict_pos[name]:
                continue
            dict_feats_noun[name].append(data_noun)
            dict_feats_verb[name].append(data_verb)
            dict_pos[name].append(chunk_nb + split_nb)
            dict_label[name] = (label_noun, label_verb)
            dict_action_label[name] = label_action
    print("Computing final results")

    input_lst = []
    print(len(dict_feats_noun))
    for i, item in enumerate(dict_feats_noun):
        input_lst.append([i, item, dict_feats_noun[item], dict_feats_verb[item], dict_label[item], dict_action_label[item]])
    from multiprocessing import Pool
    p = Pool(8)
    ans = p.map(compute_video, input_lst)
    top1_action = [x[2] for x in ans]
    top5_action = [x[3] for x in ans]
    top1_noun = [x[4] for x in ans]
    top1_verb = [x[5] for x in ans]
    top5_noun = [x[6] for x in ans]
    top5_verb = [x[7] for x in ans]
    final_top1_noun ,final_top5_noun, final_top1_verb, final_top5_verb = np.mean(top1_noun), np.mean(top5_noun), np.mean(top1_verb), np.mean(top5_verb)
    final_top1_action, final_top5_action = np.mean(top1_action), np.mean(top5_action)
    if return_result:
        pred_noun = [x[0] for x in ans]
        pred_verb = [x[1] for x in ans]
        label_noun = [x[8] for x in ans]
        label_verb = [x[9] for x in ans]
        video_ids = [x[10] for x in ans]
        conf_noun = [x[11] for x in ans]
        conf_verb = [x[12] for x in ans]
        return final_top1_action*100, final_top5_action*100, final_top1_noun*100 ,final_top5_noun*100, final_top1_verb*100, final_top5_verb*100, pred_noun, pred_verb, label_noun, label_verb, video_ids, conf_noun, conf_verb
    return final_top1_action*100, final_top5_action*100, final_top1_noun*100 ,final_top5_noun*100, final_top1_verb*100, final_top5_verb*100

def compute_video(lst):
    i, video_id, data_noun, data_verb, label, label_action = lst
    video_ids = [x for x in video_id]
    feat_noun = [x for x in data_noun]
    feat_verb = [x for x in data_verb]
    feat_noun = np.mean(feat_noun, axis=0)
    feat_verb = np.mean(feat_verb, axis=0)
    pred_noun = np.argmax(feat_noun)
    pred_verb = np.argmax(feat_verb)
    conf_noun = np.max(feat_noun)
    conf_verb = np.max(feat_verb)
    pred_action = (pred_verb * 1000) + pred_noun
    label_noun, label_verb = label
    top1_action = (int(pred_action) == int(label_action)) * 1.0
    top5_action = (int(label_noun) in np.argsort(-feat_noun)[:5] and int(label_verb) in np.argsort(-feat_verb)[:5]) * 1.0
    top1_noun = (int(pred_noun) == int(label_noun)) * 1.0
    top5_noun = (int(label_noun) in np.argsort(-feat_noun)[:5]) * 1.0
    top1_verb = (int(pred_verb) == int(label_verb)) * 1.0
    top5_verb = (int(label_verb) in np.argsort(-feat_verb)[:5]) * 1.0
    return [pred_noun, pred_verb, top1_action, top5_action, top1_noun, top1_verb, top5_noun, top5_verb, label_noun, label_verb, video_ids, conf_noun, conf_verb]

def action_accuracy(output_noun, output_verb, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)
    _, pred_noun = output_noun.topk(maxk, 1, True, True)
    _, pred_verb = output_verb.topk(maxk, 1, True, True)
    pred = (pred_verb * 1000) + pred_noun
    pred = pred.t()
    correct = pred.eq(target.reshape(1, -1).expand_as(pred))
    return [correct[:k].reshape(-1).float().sum(0) * 100. / batch_size for k in topk]
