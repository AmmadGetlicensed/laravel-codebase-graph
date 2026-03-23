<?php
namespace App\Listeners;

use App\Events\UserRegistered;
use App\Notifications\WelcomeNotification;
use Illuminate\Contracts\Queue\ShouldQueue;

class SendWelcomeEmail implements ShouldQueue
{
    public string $queue = 'emails';

    /**
     * Handle the UserRegistered event by sending a welcome email.
     *
     * @param UserRegistered $event
     */
    public function handle(UserRegistered $event): void
    {
        $event->user->notify(new WelcomeNotification());
    }
}
