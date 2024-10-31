python -u run.py   --task_name anomaly_detection_uae   --is_training 1   --root_path ./dataset/anomaly_detection/WADI/WADI2019_power_transformed  --model_id FEDformer_UAE_full_v2  --model FEDformer   --data WADI   --features S   --seq_len 15   --pred_len 15 --label_len 15   --d_model 32   --d_ff 32   --e_layers 1   --enc_in 123  --c_out 1   --batch_size 128   --train_epochs 1  --num_workers 0 --n_heads 8 --benchmark_id uae --dim_ff_dec 16 --freq s

python -u run.py   --task_name anomaly_detection_uae   --is_training 1   --root_path ./dataset/anomaly_detection/WADI/WADI2019_power_transformed  --model_id TimesNet_UAE_full_v2  --model TimesNet   --data WADI   --features S   --seq_len 15   --pred_len 0 --label_len 15   --d_model 32   --d_ff 32   --e_layers 1   --enc_in 123  --c_out 1   --batch_size 128   --train_epochs 1  --num_workers 0 --n_heads 8 --benchmark_id uae --dim_ff_dec 16 --freq s