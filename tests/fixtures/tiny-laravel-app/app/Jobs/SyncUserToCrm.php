<?php
namespace App\Jobs;

use App\Models\User;
use Illuminate\Bus\Queueable;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Bus\Dispatchable;
use Illuminate\Queue\InteractsWithQueue;
use Illuminate\Queue\SerializesModels;

class SyncUserToCrm implements ShouldQueue
{
    use Dispatchable, InteractsWithQueue, Queueable, SerializesModels;

    public string $queue = 'crm';

    public function __construct(public readonly User $user)
    {}

    /**
     * Push the newly registered user to the external CRM.
     */
    public function handle(): void
    {
        $this->user->update(['synced_at' => now()]);
    }
}
