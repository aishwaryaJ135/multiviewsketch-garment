# -*- coding: utf-8 -*-

import os
import torch
from torch.autograd import Variable
from src.networks import AlignUpdater, Decoder, Encoder, AlphaClassifier
import pytorch_lightning as pl

class GarmentModel(pl.LightningModule):
    def __init__(self):
        super().__init__()
        
        self.encoder = Encoder()
        self.freeze_module(self.encoder)
        self.alignUpdater = AlignUpdater()
        self.decoder = Decoder()
        self.alphaClassifier = AlphaClassifier()

        self.layer_norm = torch.nn.LayerNorm(512)
        self.criterion = torch.nn.L1Loss(reduction='none')
        self.criterion_alpha = torch.nn.CrossEntropyLoss()
        self.l1_reg_crit = torch.nn.L1Loss(size_average=False)

    def forward(self, img, pos_emb, xyz):
        """
        Input: 	
                img: Input sketch raster (B x num_views x 3 x 224 x 224)
                pos_emb: Positional Embedding (B x num_views x 10)
                xyz: Points to predict (B x num_views x num_points x 3)
        """
        B, num_views, num_points, _ = xyz.shape
        all_pred_sdf = []
        all_aligned_feat = torch.zeros((B, num_views, 512)).cuda()
        all_alpha = torch.zeros((B, num_views, 512)).cuda()
        all_latent_feat = torch.zeros((B, num_views, 512)).cuda()

        for vid in range(num_views):
            """ Get feature representation from image """
            img_feat = self.encoder(img[:, vid, :, :, :])

            """ Get aligned features from alignNet """
            aligned_feat, alpha = self.alignUpdater(torch.cat([
                img_feat, pos_emb[:, vid, :]], dim=-1))
            all_aligned_feat[:, vid, :] = aligned_feat
            
            """ Combine aligned features using Updater """
            if vid == 0:
                latent_feat = aligned_feat
            else:
                latent_feat = torch.nn.functional.avg_pool1d(
                    (all_aligned_feat[:, :vid, :]*1).permute(0, 2, 1), vid)[:, :, 0]
            all_latent_feat[:, vid, :] = latent_feat # Shape of latent_feat: B x 512

            """ Predict SDF using Decoder """
            _, _, num_points, _ = xyz.shape
            combined_feat = torch.cat([
                latent_feat.unsqueeze(1).repeat(1, num_points, 1),
                xyz[:, vid, :, :]], dim=-1).reshape(-1, 512+3)
            pred_sdf = self.decoder(combined_feat)
            all_pred_sdf.append(pred_sdf)

        return all_pred_sdf, all_aligned_feat, all_alpha, all_latent_feat

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=5e-4)
        return optimizer

    def training_step(self, train_batch, batch_idx):
        img, pos_emb, xyz, sdf, mask, all_azi = train_batch
        all_pred_sdf, all_aligned_feat, all_alpha, all_latent_feat = self.forward(img, pos_emb, xyz)
        num_views = sdf.shape[1]

        """ SDF Loss """
        loss = 0
        for vid in range(num_views):
            loss += (vid+1)*(self.criterion(all_pred_sdf[vid].reshape(-1, 1),
                sdf[:, vid, :, :].reshape(-1, 1)) * mask[:, vid, :, :].reshape(-1, 1)).mean()
        self.log('sdf_loss', loss)
        self.log('train_loss', loss)
        return loss

    def validation_step(self, val_batch, val_idx):
        img, pos_emb, xyz, sdf, mask, all_azi = val_batch
        all_pred_sdf, all_aligned_feat, all_alpha, all_latent_feat = self.forward(img, pos_emb, xyz)
        num_views = sdf.shape[1]

        loss = 0
        for vid in range(num_views):
            loss += (vid+1)*(self.criterion(all_pred_sdf[vid].reshape(-1, 1),
                sdf[:, vid, :, :].reshape(-1, 1)) * mask[:, vid, :, :].reshape(-1, 1)).mean()
        
        self.log('val_sdf_loss', loss)
        all_correct, total = 0, 1
        self.log('val_loss', loss)
        return all_correct, total

    def validation_epoch_end(self, validation_step_outputs):
        correct = sum([item[0] for item in validation_step_outputs])
        total = sum([item[1] for item in validation_step_outputs])
        self.log('val_alpha_acc', correct/total)

    def freeze_module(self, module):
        module.eval()
        for param in module.parameters():
            param.requires_grad = False
