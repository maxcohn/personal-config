# If you come from bash you might have to change your $PATH.
# export PATH=$HOME/bin:/usr/local/bin:$PATH

# Path to your oh-my-zsh installation.
export ZSH="$HOME/.oh-my-zsh"

ZSH_THEME="max"
#
# Uncomment the following line to enable command auto-correction.
# ENABLE_CORRECTION="true"

DISABLE_UNTRACKED_FILES_DIRTY="true"

# Which plugins would you like to load?
# Standard plugins can be found in $ZSH/plugins/
# Custom plugins may be added to $ZSH_CUSTOM/plugins/
# Example format: plugins=(rails git textmate ruby lighthouse)
# Add wisely, as too many plugins slow down shell startup.
plugins=(
	git
	zsh-autosuggestions
	zsh-syntax-highlighting
	golang
)

source $ZSH/oh-my-zsh.sh

###############################################################################
# Custom config
###############################################################################

# Turn off the beep
unsetopt BEEP

setopt histignorealldups sharehistory

# History

# Number of history entries loaded into memory
HISTSIZE=20000
# Number of history entries saved to the history file
SAVEHIST=10000000
HISTFILE=~/.zsh_history

# Load aliases
source ~/.zaliases

# Machine-specific config: untracked, never committed
[ -f ~/.zshrc.local ] && source ~/.zshrc.local

