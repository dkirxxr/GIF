import sys
sys.path.append('../../')
import constants as cnst
import os
os.environ['PYTHONHASHSEED'] = '2'
import tqdm
from model.stg2_generator import StyledGenerator
import numpy as np
from my_utils.visualize_flame_overlay import OverLayViz
from my_utils.flm_dynamic_fit_overlay import camera_ringnetpp
from my_utils.generate_gif import generate_from_flame_sequence
from my_utils.generic_utils import save_set_of_images
from my_utils import compute_fid
import constants
from dataset_loaders import fast_image_reshape
import torch
from my_utils import generic_utils
from my_utils.eye_centering import position_to_given_location


def ge_gen_in(flm_params, textured_rndr, norm_map, normal_map_cond, texture_cond):
    if normal_map_cond and texture_cond:
        return torch.cat((textured_rndr, norm_map), dim=1)
    elif normal_map_cond:
        return norm_map
    elif texture_cond:
        return textured_rndr
    else:
        return flm_params


# General settings
save_images = True
code_size = 236
use_inst_norm = True
core_tensor_res = 4
resolution = 256
alpha = 1
step_max = int(np.log2(resolution) - 2)
root_out_dir = f'{cnst.output_root}sample/'
num_smpl_to_eval_on = 1000
use_styled_conv_stylegan2 = True

flength = 5000
cam_t = np.array([0., 0., 0])
camera_params = camera_ringnetpp((512, 512), trans=cam_t, focal=flength)


run_ids_1 = [29, ]  # with sqrt(2)
# run_ids_1 = [7, 24, 8, 3]
# run_ids_1 = [7, 8, 3]

settings_for_runs = \
    {24: {'name': 'vector_cond', 'model_idx': '216000_1', 'normal_maps_as_cond': False,
          'rendered_flame_as_condition': False, 'apply_sqrt2_fac_in_eq_lin': False},
     29: {'name': 'full_model', 'model_idx': '026000_1', 'normal_maps_as_cond': True,
          'rendered_flame_as_condition': True, 'apply_sqrt2_fac_in_eq_lin': True},
     7: {'name': 'flm_rndr_tex_interp', 'model_idx': '488000_1', 'normal_maps_as_cond': False,
         'rendered_flame_as_condition': True, 'apply_sqrt2_fac_in_eq_lin': False},
     3: {'name': 'norm_mp_tex_interp', 'model_idx': '040000_1', 'normal_maps_as_cond': True,
         'rendered_flame_as_condition': False, 'apply_sqrt2_fac_in_eq_lin': False},
     8: {'name': 'norm_map_rend_flm_no_tex_interp', 'model_idx': '460000_1', 'normal_maps_as_cond': True,
         'rendered_flame_as_condition': True, 'apply_sqrt2_fac_in_eq_lin': False},}

overlay_visualizer = OverLayViz()
# overlay_visualizer.setup_renderer(mesh_file=None)

flm_params = np.zeros((num_smpl_to_eval_on, code_size)).astype('float32')
fl_param_dict = np.load(cnst.all_flame_params_file, allow_pickle=True).item()
for i, key in enumerate(fl_param_dict):
    flame_param = fl_param_dict[key]
    flame_param = np.hstack((flame_param['shape'], flame_param['exp'], flame_param['pose'], flame_param['cam'],
                             flame_param['tex'], flame_param['lit'].flatten()))
    # tz = camera_params['f'][0] / (camera_params['c'][0] * flame_param[:, 156:157])
    # flame_param[:, 156:159] = np.concatenate((flame_param[:, 157:], tz), axis=1)

    # import ipdb; ipdb.set_trace()
    flm_params[i, :] = flame_param.astype('float32')
    if i == num_smpl_to_eval_on - 1:
        break

batch_size = 64
flame_decoder = overlay_visualizer.deca.flame.eval()

for run_idx in run_ids_1:
    # import ipdb; ipdb.set_trace()
    generator_1 = torch.nn.DataParallel(
        StyledGenerator(embedding_vocab_size=69158,
                        rendered_flame_ascondition=settings_for_runs[run_idx]['rendered_flame_as_condition'],
                        normal_maps_as_cond=settings_for_runs[run_idx]['normal_maps_as_cond'],
                        core_tensor_res=core_tensor_res,
                        w_truncation_factor=1.0,
                        apply_sqrt2_fac_in_eq_lin=settings_for_runs[run_idx]['apply_sqrt2_fac_in_eq_lin'],
                        n_mlp=8)).cuda()
    model_idx = settings_for_runs[run_idx]['model_idx']
    ckpt1 = torch.load(f'{cnst.output_root}checkpoint/{run_idx}/{model_idx}.model')
    generator_1.load_state_dict(ckpt1['generator_running'])
    generator_1 = generator_1.eval()

    # images = np.zeros((num_smpl_to_eval_on, 3, resolution, resolution)).astype('float32')
    pbar = tqdm.tqdm(range(0, num_smpl_to_eval_on, batch_size))
    pbar.set_description('Generating_images')
    flame_mesh_imgs = None
    mdl_id = 'mdl2_'
    if settings_for_runs[run_idx]['name'] == 'full_model':
        mdl_id = 'mdl1_'

    for batch_idx in pbar:
        flm_batch = flm_params[batch_idx:batch_idx+batch_size, :]
        flm_batch = torch.from_numpy(flm_batch).cuda()
        flm_batch = position_to_given_location(flame_decoder, flm_batch)
        batch_size_true = flm_batch.shape[0]

        if settings_for_runs[run_idx]['normal_maps_as_cond'] or \
                settings_for_runs[run_idx]['rendered_flame_as_condition']:
            cam = flm_batch[:, constants.DECA_IDX['cam'][0]:constants.DECA_IDX['cam'][1]:]
            shape = flm_batch[:, constants.INDICES['SHAPE'][0]:constants.INDICES['SHAPE'][1]]
            exp = flm_batch[:, constants.INDICES['EXP'][0]:constants.INDICES['EXP'][1]]
            pose = flm_batch[:, constants.INDICES['POSE'][0]:constants.INDICES['POSE'][1]]
            # import ipdb; ipdb.set_trace()
            light_code = \
                flm_batch[:, constants.DECA_IDX['lit'][0]:constants.DECA_IDX['lit'][1]:].view((batch_size_true, 9, 3))
            texture_code = flm_batch[:, constants.DECA_IDX['tex'][0]:constants.DECA_IDX['tex'][1]:]
            norma_map_img, _, _, _, rend_flm = \
                overlay_visualizer.get_rendered_mesh(flame_params=(shape, exp, pose, light_code, texture_code),
                                                     camera_params=cam)
            rend_flm = torch.clamp(rend_flm, 0, 1) * 2 - 1
            norma_map_img = torch.clamp(norma_map_img, 0, 1) * 2 - 1
            rend_flm = fast_image_reshape(rend_flm, height_out=256, width_out=256, mode='bilinear')
            norma_map_img = fast_image_reshape(norma_map_img, height_out=256, width_out=256, mode='bilinear')

        else:
            rend_flm = None
            norma_map_img = None

        gen_1_in = ge_gen_in(flm_batch, rend_flm, norma_map_img, settings_for_runs[run_idx]['normal_maps_as_cond'],
                             settings_for_runs[run_idx]['rendered_flame_as_condition'])

        # torch.manual_seed(2)
        identity_embeddings = torch.randint(low=0, high=69158, size=(gen_1_in.shape[0], ), dtype=torch.long,
                                            device='cuda')

        mdl_1_gen_images = generic_utils.get_images_from_flame_params(
            flame_params=gen_1_in.cpu().numpy(), pose=None,
            model=generator_1,
            step=step_max, alpha=alpha,
            input_indices=identity_embeddings.cpu().numpy())
        # import ipdb; ipdb.set_trace()
        images = torch.clamp(mdl_1_gen_images, -1, 1).cpu().numpy()
        flame_mesh_imgs = torch.clamp(rend_flm, -1, 1).cpu().numpy()


        save_path_current_id = os.path.join(root_out_dir, 'inter_model_comparison', settings_for_runs[run_idx]['name'])
        save_set_of_images(path=save_path_current_id, prefix=f'{mdl_id}_{batch_idx}',
                           images=(images + 1) / 2, show_prog_bar=True)

        #save flam rndr
        save_path_current_id_flm_rndr = os.path.join(root_out_dir, 'inter_model_comparison',
                                                     settings_for_runs[run_idx]['name'])
        save_set_of_images(path=save_path_current_id_flm_rndr, prefix=f'mesh_{batch_idx}',
                           images=(flame_mesh_imgs + 1) / 2, show_prog_bar=True)



# save_set_of_images(path=save_path_this_expt, prefix='mesh_', images=((norma_map_img + 1) / 2).cpu().numpy())
# save_set_of_images(path=save_path_this_expt, prefix='mdl1_', images=((mdl_1_gen_images + 1) / 2).cpu().numpy())
# save_set_of_images(path=save_path_this_expt, prefix='mdl2_', images=((mdl_2_gen_images + 1) / 2).cpu().numpy())