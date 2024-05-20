project=visual
date=pot

# for seed in {1..7}
for seed in 1
do
  echo ">>" Evaluating poly for seed $seed
  CUDA_VISIBLE_DEVICES=$seed python src/eval.py \
    --molecule poly \
    --project $project \
    --date $date \
    --seed $seed \
    --start_state pp2 \
    --end_state pp1 \
    --num_samples 16 \
    --num_steps 5000 \
    --bias_scale 0.01 \
    --flexible
    sleep 1
done