from models import clip
import torch
import pandas as pd
import numpy as np
import os
from collections import OrderedDict
import json


def convert_to_token(xh):
    xh_id = clip.tokenize(xh).cpu().data.numpy()
    return xh_id


def text_prompt(dataset='HMDB51', data_path = None ,clipbackbone='ViT-B/16', device='cpu', text_finetune=None, useEncoder = False):
    actionlist, actionprompt, actiontoken = [], {}, []
    numC = {'HMDB51-feature-30fps-center': 51,}

    # load the CLIP model
    clipmodel, _ = clip.load(clipbackbone, device=device, jit=False)
    if text_finetune is not None:
        import torch.nn as nn
        clipmodel.text_projection = nn.Parameter(torch.zeros(512, 512//2))
        clipmodel.image_projection = nn.Parameter(torch.zeros(768, 512//2))
        lavila = torch.load(text_finetune, map_location='cpu')
        lavila_checkpoint = lavila['state_dict']
        new_dict = clipmodel.state_dict()
        for key in lavila_checkpoint: #allkeys들은 모두 module.으로 시작한다. visual부분을 빼기위해서 
            if not key.startswith('module.visual'):
                new_dict[key[7:]] = lavila_checkpoint[key]
        # load로 불러온 pre-trained weight를 new_dict에 담아주고
        clipmodel.load_state_dict(new_dict)
        clipmodel.to(device)
    for paramclip in clipmodel.parameters():
        paramclip.requires_grad = False
    clipmodel.eval()

    # convert to token, will automatically padded to 77 with zeros
    if dataset == 'HMDB51-feature-30fps-center':
        meta = open("../data/HMDB51/HMDB51_action.list", 'rb')
        actionlist = meta.readlines()
        meta.close()
        actionlist = np.array([a.decode('utf-8').split('\n')[0] for a in actionlist])
        actiontoken = np.array([convert_to_token(a) for a in actionlist])
    # More datasets to be continued

    elif dataset == 'EPIC':
        noun_anno_path = os.path.join(data_path, 'epic100_noun_classes.csv')
        verb_anno_path = os.path.join(data_path, 'epic100_verb_classes.csv')
        action_anno_path = os.path.join(data_path, 'epic100_action_classes.csv')
        noun_cleaned = pd.read_csv(noun_anno_path, header=None, delimiter=',')
        verb_cleaned = pd.read_csv(verb_anno_path, header=None, delimiter=',')
        action_cleaned = pd.read_csv(action_anno_path, header=None, delimiter=',')
        nounlist = list(noun_cleaned.values[:, 0])
        verblist = list(verb_cleaned.values[:, 0])
        actionlist = list(action_cleaned.values[:, 0])
        nountoken = np.array([convert_to_token(a) for a in nounlist])
        verbtoken = np.array([convert_to_token(a) for a in verblist])
        actiontoken = np.array([convert_to_token(a) for a in actionlist])
    
        # query the vector from dictionary
        with torch.no_grad():
            nounembed = clipmodel.encode_text_light(torch.tensor(nountoken).to(device))
            verbembed = clipmodel.encode_text_light(torch.tensor(verbtoken).to(device))
            actionembed = clipmodel.encode_text_light(torch.tensor(actiontoken).to(device))
                
        noundict = OrderedDict((nounlist[i], nounembed[i].cpu().data.numpy()) for i in range(300))
        verbdict = OrderedDict((verblist[i], verbembed[i].cpu().data.numpy()) for i in range(97))
        actiondict = OrderedDict((actionlist[i], actionembed[i].cpu().data.numpy()) for i in range(29100))
        nountoken = OrderedDict((nounlist[i], nountoken[i]) for i in range(300))
        verbtoken = OrderedDict((verblist[i], verbtoken[i]) for i in range(97))
        
        if useEncoder:
            actionFeatures = []
            with torch.no_grad():
                clipmodel.half()
                batch_size = 1000
                for i in range(0, actionembed.size(0), batch_size):
                    end_idx = min(i+batch_size, actionembed.size(0))
                    actionFeature = clipmodel.encode_text(actionembed.squeeze(1)[i:end_idx], torch.from_numpy(actiontoken.squeeze(1)[i:end_idx]))
                    actionFeatures.append(actionFeature)
                actiondict = torch.cat(actionFeatures, dim=0)
        
        actiontoken = OrderedDict((actionlist[i], actiontoken[i]) for i in range(29100))
        del clipmodel
        torch.cuda.empty_cache()
        
        return [nounlist, noundict, nountoken, verblist, verbdict, verbtoken, actionlist, actiondict, actiontoken]
    
    elif dataset == 'SSV2':
        action_anno_path = os.path.join(data_path, 'labels.json')
        with open(action_anno_path, 'r') as f:
            action_cleaned = json.load(f)
            actionlist = list(action_cleaned.keys())
        actiontoken = np.array([convert_to_token(a) for a in actionlist])
    
        # query the vector from dictionary
        with torch.no_grad():
            actionembed = clipmodel.encode_text_light(torch.tensor(actiontoken).to(device))
                
        actiondict = OrderedDict((actionlist[i], actionembed[i].cpu().data.numpy()) for i in range(len(actionlist)))
        
        if useEncoder:
            actionFeatures = []
            with torch.no_grad():
                clipmodel.half()
                batch_size = 1000
                for i in range(0, actionembed.size(0), batch_size):
                    end_idx = min(i+batch_size, actionembed.size(0))
                    actionFeature = clipmodel.encode_text(actionembed.squeeze(1)[i:end_idx], torch.from_numpy(actiontoken.squeeze(1)[i:end_idx]))
                    actionFeatures.append(actionFeature)
                actiondict = torch.cat(actionFeatures, dim=0)
        
        actiontoken = OrderedDict((actionlist[i], actiontoken[i]) for i in range(len(actionlist)))
        del clipmodel
        torch.cuda.empty_cache()
        
        return [actionlist, actiondict, actiontoken]
    
    elif dataset == 'Kinetics-400':
        action_anno_path = os.path.join(data_path, 'kinetics400_labels.csv')
        action_cleaned = pd.read_csv(action_anno_path, header=0, delimiter=',')
        actionlist = list(action_cleaned.values[:, 1])
        actiontoken = np.array([convert_to_token(a) for a in actionlist])
    
        # query the vector from dictionary
        with torch.no_grad():
            actionembed = clipmodel.encode_text_light(torch.tensor(actiontoken).to(device))
                
        actiondict = OrderedDict((actionlist[i], actionembed[i].cpu().data.numpy()) for i in range(len(actionlist)))
        
        if useEncoder:
            actionFeatures = []
            with torch.no_grad():
                clipmodel.half()
                batch_size = 1000
                for i in range(0, actionembed.size(0), batch_size):
                    end_idx = min(i+batch_size, actionembed.size(0))
                    actionFeature = clipmodel.encode_text(actionembed.squeeze(1)[i:end_idx], torch.from_numpy(actiontoken.squeeze(1)[i:end_idx]))
                    actionFeatures.append(actionFeature)
                actiondict = torch.cat(actionFeatures, dim=0)
        
        actiontoken = OrderedDict((actionlist[i], actiontoken[i]) for i in range(len(actionlist)))
        del clipmodel
        torch.cuda.empty_cache()
        
        return [actionlist, actiondict, actiontoken]
    
    elif dataset == 'diving-48':
        anno_path = os.path.join(data_path, 'class.csv')
        cleaned = pd.read_csv(anno_path, header=None, delimiter=',')
        # list_0 = list(cleaned.values[:, 0])
        # list_1 = list(cleaned.values[:, 1])
        # list_2 = list(cleaned.values[:, 2])
        # list_3 = list(cleaned.values[:, 3])
        actionlist = list(cleaned.values[:, 4])
        actiontoken = np.array([convert_to_token(a) for a in actionlist])
    
        # query the vector from dictionary
        with torch.no_grad():
            actionembed = clipmodel.encode_text_light(torch.tensor(actiontoken).to(device))

        actiondict = OrderedDict((actionlist[i], actionembed[i].cpu().data.numpy()) for i in range(len(actionlist)))
        
        if useEncoder:
            actionFeatures = []
            with torch.no_grad():
                clipmodel.half()
                batch_size = 1000
                for i in range(0, actionembed.size(0), batch_size):
                    end_idx = min(i+batch_size, actionembed.size(0))
                    actionFeature = clipmodel.encode_text(actionembed.squeeze(1)[i:end_idx], torch.from_numpy(actiontoken.squeeze(1)[i:end_idx]))
                    actionFeatures.append(actionFeature)
                actiondict = torch.cat(actionFeatures, dim=0)
        actiontoken = OrderedDict((actionlist[i], actiontoken[i]) for i in range(len(actionlist)))
        del clipmodel
        torch.cuda.empty_cache()
        
        return [actionlist, actiondict, actiontoken]
    
    # query the vector from dictionary
    with torch.no_grad():
        actionembed = clipmodel.encode_text_light(torch.tensor(actiontoken).to(device))

    actiondict = OrderedDict((actionlist[i], actionembed[i].cpu().data.numpy()) for i in range(numC[dataset]))
    actiontoken = OrderedDict((actionlist[i], actiontoken[i]) for i in range(numC[dataset]))
    
    del clipmodel
    torch.cuda.empty_cache()
    
    return [actionlist, actiondict, actiontoken]