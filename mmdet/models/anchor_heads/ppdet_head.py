import torch
import torch.nn as nn
from mmcv.cnn import normal_init
from mmdet.ops import DeformConv
from mmdet.core import multi_apply, multiclass_nms
from ..builder import build_loss
from ..registry import HEADS
from ..utils import bias_init_with_prob, ConvModule
INF = 1e8


class FeatureAlign(nn.Module):
    """Feature Alignment Module.

    Feature Alignment Module is implemented based on DCN v1.
    It uses anchor shape prediction rather than feature map to
    predict offsets of deformable conv layer.

    Args:
        in_channels (int): Number of channels in the input feature map.
        out_channels (int): Number of channels in the output feature map.
        kernel_size (int): Deformable conv kernel size.
        deformable_groups (int): Deformable conv group size.
    """

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size=3,
                 deformable_groups=4):
        super(FeatureAlign, self).__init__()
        offset_channels = kernel_size * kernel_size * 2
        self.conv_offset = nn.Conv2d(4,
                                     deformable_groups * offset_channels,
                                     1,
                                     bias=False)
        self.conv_adaption = DeformConv(in_channels,
                                        out_channels,
                                        kernel_size=kernel_size,
                                        padding=(kernel_size - 1) // 2,
                                        deformable_groups=deformable_groups)
        self.relu = nn.ReLU(inplace=True)

    def init_weights(self):
        normal_init(self.conv_offset, std=0.1)
        normal_init(self.conv_adaption, std=0.01)

    def forward(self, x, shape):
        offset = self.conv_offset(shape)
        x = self.relu(self.conv_adaption(x, offset))
        return x

@HEADS.register_module
class PPDetHead(nn.Module):

    def __init__(self,
                 num_classes,
                 in_channels,
                 feat_channels=256,
                 stacked_convs=4,
                 strides=(4, 8, 16, 32, 64),
                 base_edge_list=(16, 32, 64, 126, 256),
                 scale_ranges=((8, 32), (16, 64), (32, 128), (64, 256), (128, 512)),
                 sigma = 0.4,
                 with_deform=False,
                 deformable_groups=4,
                 loss_cls=None,
                 loss_bbox=None,
                 conv_cfg=None,
                 norm_cfg=None):
        super(PPDetHead, self).__init__()
        self.num_classes = num_classes
        self.cls_out_channels = num_classes - 1
        self.in_channels = in_channels
        self.feat_channels = feat_channels
        self.stacked_convs = stacked_convs
        self.strides = strides
        self.base_edge_list = base_edge_list
        self.scale_ranges = scale_ranges
        self.sigma = sigma
        self.with_deform = with_deform
        self.deformable_groups = deformable_groups
        self.loss_cls = build_loss(loss_cls)
        self.loss_bbox = build_loss(loss_bbox)
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg
        self._init_layers()

    def _init_layers(self):
        self.cls_convs = nn.ModuleList()
        self.reg_convs = nn.ModuleList()
        # box branch
        for i in range(self.stacked_convs):
            chn = self.in_channels if i == 0 else self.feat_channels
            self.reg_convs.append(
                ConvModule(
                    chn,
                    self.feat_channels,
                    3,
                    stride=1,
                    padding=1,
                    conv_cfg=self.conv_cfg,
                    norm_cfg=self.norm_cfg,
                    bias=self.norm_cfg is None))
        self.ppdet_reg = nn.Conv2d(self.feat_channels, 4, 3, padding=1)
        # cls branch
        if not self.with_deform:
            for i in range(self.stacked_convs):
                chn = self.in_channels if i == 0 else self.feat_channels
                self.cls_convs.append(
                    ConvModule(
                        chn,
                        self.feat_channels,
                        3,
                        stride=1,
                        padding=1,
                        conv_cfg=self.conv_cfg,
                        norm_cfg=self.norm_cfg,
                        bias=self.norm_cfg is None))
            self.ppdet_cls = nn.Conv2d(
                self.feat_channels, self.cls_out_channels, 3, padding=1)
        else:
            self.cls_convs.append(
                ConvModule(
                    self.feat_channels,
                    (self.feat_channels * 4),
                    3,
                    stride=1,
                    padding=1,
                    conv_cfg=self.conv_cfg,
                    norm_cfg=self.norm_cfg,
                    bias=self.norm_cfg is None))

            self.cls_convs.append(
                ConvModule(
                    (self.feat_channels * 4),
                    (self.feat_channels * 4),
                    1,
                    stride=1,
                    padding=0,
                    conv_cfg=self.conv_cfg,
                    norm_cfg=self.norm_cfg,
                    bias=self.norm_cfg is None))

            self.feature_adaption = FeatureAlign(
                self.feat_channels,
                self.feat_channels,
                kernel_size=3,
                deformable_groups=self.deformable_groups)

            self.ppdet_cls = nn.Conv2d(
                int(self.feat_channels * 4), self.cls_out_channels, 3, padding=1)

    def init_weights(self):
        for m in self.cls_convs:
            normal_init(m.conv, std=0.01)
        for m in self.reg_convs:
            normal_init(m.conv, std=0.01)
        bias_cls = bias_init_with_prob(0.01)
        normal_init(self.ppdet_cls, std=0.01, bias=bias_cls)
        normal_init(self.ppdet_reg, std=0.01)
        if self.with_deform:
            self.feature_adaption.init_weights()

    def forward(self, feats):
        return multi_apply(self.forward_single, feats)

    def forward_single(self, x):
        cls_feat = x
        reg_feat = x
        for reg_layer in self.reg_convs:
            reg_feat = reg_layer(reg_feat)
        bbox_pred = self.ppdet_reg(reg_feat)
        if self.with_deform:
            cls_feat = self.feature_adaption(cls_feat, bbox_pred.exp())
        for cls_layer in self.cls_convs:
            cls_feat = cls_layer(cls_feat)
        cls_score = self.ppdet_cls(cls_feat)
        return cls_score, bbox_pred

    def get_points(self, featmap_sizes, dtype, device, flatten=False):
        points = []
        for featmap_size in featmap_sizes:
            x_range = torch.arange(featmap_size[1], dtype=dtype, device=device) + 0.5
            y_range = torch.arange(featmap_size[0], dtype=dtype, device=device) + 0.5
            y, x = torch.meshgrid(y_range, x_range)
            if flatten:
                points.append((y.flatten(), x.flatten()))
            else:
                points.append((y, x))

        return points

    def loss(self,
             cls_scores,
             bbox_preds,
             gt_bbox_list,
             gt_label_list,
             img_metas,
             cfg,
             gt_bboxes_ignore=None):
        assert len(cls_scores) == len(bbox_preds)
        featmap_sizes = [featmap.size()[-2:] for featmap in
                         cls_scores]
        points = self.get_points(featmap_sizes, bbox_preds[0].dtype,
                                 bbox_preds[0].device)


        all_gt_label_list = torch.cat([x for x in gt_label_list])
        temp = torch.tensor(0).cuda()
        label_size_list = []
        for x in gt_label_list:
            label_size_list.append(temp)
            temp = torch.tensor(x.size()).cuda() + label_size_list[-1]

        label_list, bbox_target_list, gt_ids_list = multi_apply(
            self.ppdet_target_single,
            gt_bbox_list,
            gt_label_list,
            label_size_list,
            featmap_size_list=featmap_sizes,
            point_list=points)

        flatten_labels = [
            torch.cat([labels_level_img.flatten()
                       for labels_level_img in labels_level])
            for labels_level in zip(*label_list)
        ]
        flatten_bbox_targets = [
            torch.cat([bbox_targets_level_img.reshape(-1, 4)
                       for bbox_targets_level_img in bbox_targets_level])
            for bbox_targets_level in zip(*bbox_target_list)
        ]
        flatten_ids = [
            torch.cat([gt_ids_level_img.flatten()
                       for gt_ids_level_img in gt_ids_level])
            for gt_ids_level in zip(*gt_ids_list)
        ]

        flatten_labels = torch.cat(flatten_labels)
        flatten_bbox_targets = torch.cat(flatten_bbox_targets)
        flatten_ids = torch.cat(flatten_ids)
        num_imgs = cls_scores[0].size(0)
        flatten_cls_scores = [
            cls_score.permute(0, 2, 3, 1).reshape(-1, self.cls_out_channels)
            for cls_score in cls_scores
        ]
        flatten_bbox_preds = [
            bbox_pred.permute(0, 2, 3, 1).reshape(-1, 4)
            for bbox_pred in bbox_preds
        ]
        flatten_cls_scores = torch.cat(flatten_cls_scores)
        flatten_bbox_preds = torch.cat(flatten_bbox_preds)
        pos_inds = (flatten_labels > 0).nonzero().view(-1)
        num_pos = len(pos_inds)


        neg_inds = (flatten_labels <= 0).nonzero().view(-1)
        agg_labels = flatten_labels[neg_inds]
        agg_cls_scores = flatten_cls_scores[neg_inds]
        num_agg_pos = 0

        for i, class_id in enumerate(all_gt_label_list):

            aggregation_indices = (flatten_ids == i).nonzero()

            if flatten_labels[aggregation_indices].size()[0] != 0:
                agg_labels = torch.cat((all_gt_label_list[i:i+1], agg_labels))
                agg_cls_scores = torch.cat((flatten_cls_scores[aggregation_indices].mean(dim=0), agg_cls_scores))
                num_agg_pos +=1

        loss_cls = self.loss_cls(agg_cls_scores, agg_labels, avg_factor=num_agg_pos + num_imgs)

        if num_pos > 0:
            pos_bbox_preds = flatten_bbox_preds[pos_inds]
            pos_bbox_targets = flatten_bbox_targets[pos_inds]
            pos_weights = pos_bbox_targets.new_zeros(pos_bbox_targets.size())+1.0
            loss_bbox = self.loss_bbox(pos_bbox_preds,
                pos_bbox_targets, pos_weights, avg_factor = num_pos)
        else:
            loss_bbox = torch.tensor([0], dtype=flatten_bbox_preds.dtype, device=flatten_bbox_preds.device)
        return dict(
            loss_cls=loss_cls,
            loss_bbox=loss_bbox)

    def ppdet_target_single(self,
                            gt_bboxes_raw,
                            gt_labels_raw,
                            label_size_list_raw,
                            featmap_size_list=None,
                            point_list=None):

        gt_areas = torch.sqrt((gt_bboxes_raw[:, 2] - gt_bboxes_raw[:, 0]) * (
                gt_bboxes_raw[:, 3] - gt_bboxes_raw[:, 1]))
        label_list = []
        bbox_target_list = []
        ids_list = []
        for base_len, (lower_bound, upper_bound), stride, featmap_size, (y, x) \
                in zip(self.base_edge_list, self.scale_ranges, self.strides, featmap_size_list, point_list):
            labels = gt_labels_raw.new_zeros(featmap_size)
            bbox_targets = gt_bboxes_raw.new(featmap_size[0], featmap_size[1], 4) + 1
            gt_ids = gt_labels_raw.new_zeros(featmap_size) - 1
            hit_indices = ((gt_areas >= lower_bound) & (gt_areas <= upper_bound)).nonzero().flatten()
            if len(hit_indices) == 0:
                label_list.append(labels)
                bbox_target_list.append(torch.log(bbox_targets))
                ids_list.append(gt_ids)
                continue

            _, hit_index_order = torch.sort(-gt_areas[hit_indices])
            hit_indices = hit_indices[hit_index_order]
            gt_bboxes = gt_bboxes_raw[hit_indices, :] / stride
            gt_labels = gt_labels_raw[hit_indices]
            half_w = 0.5 * (gt_bboxes[:, 2] - gt_bboxes[:, 0])
            half_h = 0.5 * (gt_bboxes[:, 3] - gt_bboxes[:, 1])
            pos_left = torch.ceil(gt_bboxes[:, 0] + (1 - self.sigma) * half_w - 0.5).long().\
                clamp(0, featmap_size[1] - 1)
            pos_right = torch.floor(gt_bboxes[:, 0] + (1 + self.sigma) * half_w - 0.5).long().\
                clamp(0, featmap_size[1] - 1)
            pos_top = torch.ceil(gt_bboxes[:, 1] + (1 - self.sigma) * half_h - 0.5).long().\
                clamp(0, featmap_size[0] - 1)
            pos_down = torch.floor(gt_bboxes[:, 1] + (1 + self.sigma) * half_h - 0.5).long().\
                clamp(0, featmap_size[0] - 1)
            for px1, py1, px2, py2, label, gt_id, (gt_x1, gt_y1, gt_x2, gt_y2) in \
                    zip(pos_left, pos_top, pos_right, pos_down, gt_labels, hit_indices,
                        gt_bboxes_raw[hit_indices, :]):
                labels[py1:py2 + 1, px1:px2 + 1] = label
                gt_ids[py1:py2 + 1, px1:px2 + 1] = gt_id + label_size_list_raw
                bbox_targets[py1:py2 + 1, px1:px2 + 1, 0] = (stride * x[py1:py2 + 1, px1:px2 + 1] - gt_x1) / base_len
                bbox_targets[py1:py2 + 1, px1:px2 + 1, 1] = (stride * y[py1:py2 + 1, px1:px2 + 1] - gt_y1) / base_len
                bbox_targets[py1:py2 + 1, px1:px2 + 1, 2] = (gt_x2 - stride * x[py1:py2 + 1, px1:px2 + 1]) / base_len
                bbox_targets[py1:py2 + 1, px1:px2 + 1, 3] = (gt_y2 - stride * y[py1:py2 + 1, px1:px2 + 1]) / base_len
            bbox_targets = bbox_targets.clamp(min=1./16, max=16.)
            label_list.append(labels)
            bbox_target_list.append(torch.log(bbox_targets))
            ids_list.append(gt_ids)
        return label_list, bbox_target_list, ids_list

    def get_bboxes(self,
                   cls_scores,
                   bbox_preds,
                   img_metas,
                   cfg,
                   rescale=None):
        assert len(cls_scores) == len(bbox_preds)
        num_levels = len(cls_scores)
        featmap_sizes = [featmap.size()[-2:] for featmap in cls_scores]
        points = self.get_points(featmap_sizes, bbox_preds[0].dtype,
                                 bbox_preds[0].device, flatten=True)
        result_list = []
        for img_id in range(len(img_metas)):
            cls_score_list = [
                cls_scores[i][img_id].detach() for i in range(num_levels)
            ]
            bbox_pred_list = [
                bbox_preds[i][img_id].detach() for i in range(num_levels)
            ]
            img_shape = img_metas[img_id]['img_shape']
            scale_factor = img_metas[img_id]['scale_factor']
            det_bboxes = self.get_bboxes_single(cls_score_list, bbox_pred_list, featmap_sizes, points,
                                                img_shape, scale_factor, cfg, rescale)
            result_list.append(det_bboxes)
        return result_list

    def get_bboxes_aug(self,
                   cls_scores,
                   bbox_preds,
                   img_metas,
                   cfg,
                   rescale=None):
        assert len(cls_scores) == len(bbox_preds)
        num_levels = len(cls_scores)
        featmap_sizes = [featmap.size()[-2:] for featmap in cls_scores]
        points = self.get_points(featmap_sizes, bbox_preds[0].dtype,
                                 bbox_preds[0].device, flatten=True)
        result_list = []
        for img_id in range(len(img_metas)):
            cls_score_list = [
                cls_scores[i][img_id].detach() for i in range(num_levels)
            ]
            bbox_pred_list = [
                bbox_preds[i][img_id].detach() for i in range(num_levels)
            ]
            img_shape = img_metas[img_id]['img_shape']
            scale_factor = img_metas[img_id]['scale_factor']
            det_bboxes = self.get_bboxes_single_aug(cls_score_list, bbox_pred_list, featmap_sizes, points,
                                                img_shape, scale_factor, cfg, rescale)
            result_list.append(det_bboxes)

        return result_list

    def get_bboxes_single_aug(self,
                          cls_scores,
                          bbox_preds,
                          featmap_sizes,
                          point_list,
                          img_shape,
                          scale_factor,
                          cfg,
                          rescale=False, debug=False):
        assert len(cls_scores) == len(bbox_preds) == len(point_list)
        det_bboxes = []
        det_scores = []
        for cls_score, bbox_pred, featmap_size, stride, base_len, (y, x) in zip(
                cls_scores, bbox_preds, featmap_sizes, self.strides, self.base_edge_list, point_list):

            assert cls_score.size()[-2:] == bbox_pred.size()[-2:]
            scores = cls_score.permute(1, 2, 0).reshape(
                -1, self.cls_out_channels).sigmoid()
            bbox_pred = bbox_pred.permute(1, 2, 0).reshape(-1, 4).exp()
            nms_pre = cfg.get('nms_pre', -1)
            if nms_pre > 0 and scores.shape[0] > nms_pre:
                max_scores, _ = scores.max(dim=1)
                _, topk_inds = max_scores.topk(nms_pre)
                bbox_pred = bbox_pred[topk_inds, :]
                scores = scores[topk_inds, :]
                y = y[topk_inds]
                x = x[topk_inds]
            x1 = (stride * x - base_len * bbox_pred[:, 0]).clamp(min=0, max=img_shape[1] - 1)
            y1 = (stride * y - base_len * bbox_pred[:, 1]).clamp(min=0, max=img_shape[0] - 1)
            x2 = (stride * x + base_len * bbox_pred[:, 2]).clamp(min=0, max=img_shape[1] - 1)
            y2 = (stride * y + base_len * bbox_pred[:, 3]).clamp(min=0, max=img_shape[0] - 1)
            bboxes = torch.stack([x1, y1, x2, y2], -1)
            det_bboxes.append(bboxes)
            det_scores.append(scores)

        det_bboxes = torch.cat(det_bboxes)

        if rescale:
            det_bboxes /= det_bboxes.new_tensor(scale_factor)

        det_scores = torch.cat(det_scores)
        padding = det_scores.new_zeros(det_scores.shape[0], 1)
        det_scores = torch.cat([padding, det_scores], dim=1)

        return det_bboxes, det_scores


    def get_bboxes_single(self,
                          cls_scores,
                          bbox_preds,
                          featmap_sizes,
                          point_list,
                          img_shape,
                          scale_factor,
                          cfg,
                          rescale=False, debug=False):
        assert len(cls_scores) == len(bbox_preds) == len(point_list)
        det_bboxes = []
        det_scores = []
        for cls_score, bbox_pred, featmap_size, stride, base_len, (y, x) in zip(
                cls_scores, bbox_preds, featmap_sizes, self.strides, self.base_edge_list, point_list):

            assert cls_score.size()[-2:] == bbox_pred.size()[-2:]
            scores = cls_score.permute(1, 2, 0).reshape(
                -1, self.cls_out_channels).sigmoid()
            bbox_pred = bbox_pred.permute(1, 2, 0).reshape(-1, 4).exp()
            nms_pre = cfg.get('nms_pre', -1)
            if nms_pre > 0 and scores.shape[0] > nms_pre:
                max_scores, _ = scores.max(dim=1)
                _, topk_inds = max_scores.topk(nms_pre)
                bbox_pred = bbox_pred[topk_inds, :]
                scores = scores[topk_inds, :]
                y = y[topk_inds]
                x = x[topk_inds]
            x1 = (stride * x - base_len * bbox_pred[:, 0]).clamp(min=0, max=img_shape[1] - 1)
            y1 = (stride * y - base_len * bbox_pred[:, 1]).clamp(min=0, max=img_shape[0] - 1)
            x2 = (stride * x + base_len * bbox_pred[:, 2]).clamp(min=0, max=img_shape[1] - 1)
            y2 = (stride * y + base_len * bbox_pred[:, 3]).clamp(min=0, max=img_shape[0] - 1)
            bboxes = torch.stack([x1, y1, x2, y2], -1)
            det_bboxes.append(bboxes)
            det_scores.append(scores)
        det_bboxes = torch.cat(det_bboxes)
        if rescale:
            det_bboxes /= det_bboxes.new_tensor(scale_factor)
        det_scores = torch.cat(det_scores)
        padding = det_scores.new_zeros(det_scores.shape[0], 1)
        det_scores = torch.cat([padding, det_scores], dim=1)
        if debug:
            det_bboxes, det_labels = multiclass_nms(
                det_bboxes,
                det_scores,
                cfg['k'],
                cfg['agg_thr'],
                cfg['score_thr'],
                cfg['nms'],
                cfg['max_per_img'])
        else:
            det_bboxes, det_labels = multiclass_nms(
                det_bboxes,
                det_scores,
                cfg.score_thr,
                cfg.k,
                cfg.agg_thr,
                cfg.nms,
                cfg.max_per_img)
        return det_bboxes, det_labels
